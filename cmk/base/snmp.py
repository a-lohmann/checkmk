#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import os
import subprocess
import sys
from contextlib import suppress
from typing import Dict, Iterable, List, Optional, Tuple
from pathlib import Path

from six import ensure_binary, ensure_str

import cmk.utils.debug
import cmk.utils.snmp_cache as snmp_cache
import cmk.utils.tty as tty
from cmk.utils.exceptions import MKBailOut, MKGeneralException
from cmk.utils.log import console
from cmk.utils.type_defs import (
    ABCSNMPBackend,
    DecodedString,
    HostName,
    OID,
    RawValue,
    SNMPHostConfig,
    SNMPRowInfo,
)

import cmk.base.cleanup
import cmk.base.config as config
import cmk.base.ip_lookup as ip_lookup
from cmk.fetchers import factory
from cmk.fetchers.snmp_backend import StoredWalkSNMPBackend

try:
    from cmk.fetchers.cee.snmp_backend import inline  # pylint: disable=ungrouped-imports
except ImportError:
    inline = None  # type: ignore[assignment]

SNMPRowInfoForStoredWalk = List[Tuple[OID, str]]
SNMPWalkOptions = Dict[str, List[OID]]

#.
#   .--Generic SNMP--------------------------------------------------------.
#   |     ____                      _        ____  _   _ __  __ ____       |
#   |    / ___| ___ _ __   ___ _ __(_) ___  / ___|| \ | |  \/  |  _ \      |
#   |   | |  _ / _ \ '_ \ / _ \ '__| |/ __| \___ \|  \| | |\/| | |_) |     |
#   |   | |_| |  __/ | | |  __/ |  | | (__   ___) | |\  | |  | |  __/      |
#   |    \____|\___|_| |_|\___|_|  |_|\___| |____/|_| \_|_|  |_|_|         |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Top level functions to realize SNMP functionality for Check_MK.      |
#   '----------------------------------------------------------------------'


def create_snmp_host_config(hostname):
    # type: (str) -> SNMPHostConfig
    host_config = config.get_config_cache().get_host_config(hostname)

    # ip_lookup.lookup_ipv4_address() returns Optional[str] in general, but for
    # all cases that reach the code here we seem to have "str".
    address = ip_lookup.lookup_ip_address(hostname)
    if address is None:
        raise MKGeneralException("Failed to gather IP address of %s" % hostname)

    return host_config.snmp_config(address)


# Contextes can only be used when check_plugin_name is given.
def get_single_oid(snmp_config, oid, check_plugin_name=None, do_snmp_scan=True, *, backend):
    # type: (SNMPHostConfig, str, Optional[str], bool, ABCSNMPBackend) -> Optional[DecodedString]
    # The OID can end with ".*". In that case we do a snmpgetnext and try to
    # find an OID with the prefix in question. The *cache* is working including
    # the X, however.
    if oid[0] != '.':
        if cmk.utils.debug.enabled():
            raise MKGeneralException("OID definition '%s' does not begin with a '.'" % oid)
        oid = '.' + oid

    # TODO: Use generic cache mechanism
    if snmp_cache.is_in_single_oid_cache(oid):
        console.vverbose("       Using cached OID %s: " % oid)
        cached_value = snmp_cache.get_oid_from_single_oid_cache(oid)
        console.vverbose("%s%s%r%s\n" % (tty.bold, tty.green, cached_value, tty.normal))
        return cached_value

    # get_single_oid() can only return a single value. When SNMPv3 is used with multiple
    # SNMP contexts, all contextes will be queried until the first answer is received.
    console.vverbose("       Getting OID %s: " % oid)
    for context_name in snmp_config.snmpv3_contexts_of(check_plugin_name):
        try:
            value = backend.get(
                snmp_config,
                oid=oid,
                context_name=context_name,
            )

            if value is not None:
                break  # Use first received answer in case of multiple contextes
        except Exception:
            if cmk.utils.debug.enabled():
                raise
            value = None

    if value is not None:
        console.vverbose("%s%s%r%s\n" % (tty.bold, tty.green, value, tty.normal))
    else:
        console.vverbose("failed.\n")

    if value is not None:
        decoded_value = snmp_config.ensure_str(value)  # type: Optional[DecodedString]
    else:
        decoded_value = value

    snmp_cache.set_single_oid_cache(oid, decoded_value)
    return decoded_value


def walk_for_export(snmp_config, oid):
    # type: (SNMPHostConfig, OID) -> SNMPRowInfoForStoredWalk
    rows = StoredWalkSNMPBackend().walk(snmp_config, oid=oid)
    return _convert_rows_for_stored_walk(rows)


#.
#   .--SNMP helpers--------------------------------------------------------.
#   |     ____  _   _ __  __ ____    _          _                          |
#   |    / ___|| \ | |  \/  |  _ \  | |__   ___| |_ __   ___ _ __ ___      |
#   |    \___ \|  \| | |\/| | |_) | | '_ \ / _ \ | '_ \ / _ \ '__/ __|     |
#   |     ___) | |\  | |  | |  __/  | | | |  __/ | |_) |  __/ |  \__ \     |
#   |    |____/|_| \_|_|  |_|_|     |_| |_|\___|_| .__/ \___|_|  |___/     |
#   |                                            |_|                       |
#   +----------------------------------------------------------------------+
#   | Internal helpers for processing SNMP things                          |
#   '----------------------------------------------------------------------'


def _convert_rows_for_stored_walk(rows):
    # type: (SNMPRowInfo) -> SNMPRowInfoForStoredWalk
    def should_be_encoded(v):
        # type: (RawValue) -> bool
        for c in bytearray(v):
            if c < 32 or c > 127:
                return True
        return False

    def hex_encode_value(v):
        # type: (RawValue) -> str
        encoded = ""
        for c in bytearray(v):
            encoded += "%02X " % c
        return "\"%s\"" % encoded

    new_rows = []  # type: SNMPRowInfoForStoredWalk
    for oid, value in rows:
        if value == b"ENDOFMIBVIEW":
            continue

        if should_be_encoded(value):
            new_rows.append((oid, hex_encode_value(value)))
        else:
            new_rows.append((oid, ensure_str(value)))
    return new_rows


#.
#   .--Main modes----------------------------------------------------------.
#   |       __  __       _                             _                   |
#   |      |  \/  | __ _(_)_ __    _ __ ___   ___   __| | ___  ___         |
#   |      | |\/| |/ _` | | '_ \  | '_ ` _ \ / _ \ / _` |/ _ \/ __|        |
#   |      | |  | | (_| | | | | | | | | | | | (_) | (_| |  __/\__ \        |
#   |      |_|  |_|\__,_|_|_| |_| |_| |_| |_|\___/ \__,_|\___||___/        |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   | Some main modes to help the user                                     |
#   '----------------------------------------------------------------------'


def do_snmptranslate(walk_filename):
    # type: (str) -> None
    if not walk_filename:
        raise MKGeneralException("Please provide the name of a SNMP walk file")

    walk_path = "%s/%s" % (cmk.utils.paths.snmpwalks_dir, walk_filename)
    if not os.path.exists(walk_path):
        raise MKGeneralException("The walk '%s' does not exist" % walk_path)

    def translate(lines):
        # type: (List[bytes]) -> List[Tuple[bytes, bytes]]
        result_lines = []
        try:
            oids_for_command = []
            for line in lines:
                oids_for_command.append(line.split(b" ")[0])

            command = [b"snmptranslate", b"-m", b"ALL",
                       b"-M+%s" % cmk.utils.paths.local_mib_dir] + oids_for_command
            p = subprocess.Popen(command,
                                 stdout=subprocess.PIPE,
                                 stderr=open(os.devnull, "w"),
                                 close_fds=True)
            p.wait()
            if p.stdout is None:
                raise RuntimeError()
            output = p.stdout.read()
            result = output.split(b"\n")[0::2]
            for idx, line in enumerate(result):
                result_lines.append((line.strip(), lines[idx].strip()))

        except Exception as e:
            console.error("%s\n" % e)

        return result_lines

    # Translate n-oid's per cycle
    entries_per_cycle = 500
    translated_lines = []  # type: List[Tuple[bytes, bytes]]

    walk_lines = open(walk_path).readlines()
    console.error("Processing %d lines.\n" % len(walk_lines))

    i = 0
    while i < len(walk_lines):
        console.error("\r%d to go...    " % (len(walk_lines) - i))
        process_lines = walk_lines[i:i + entries_per_cycle]
        # FIXME: This encoding ping-pong os horrible...
        translated = translate([ensure_binary(pl) for pl in process_lines])
        i += len(translated)
        translated_lines += translated
    console.error("\rfinished.                \n")

    with suppress(IOError):
        sys.stdout.write("\n".join("%s --> %s" % (ensure_str(line), ensure_str(translation))
                                   for translation, line in translated_lines) + "\n")


def do_snmpwalk(options, hostnames):
    # type: (SNMPWalkOptions, List[HostName]) -> None
    if "oids" in options and "extraoids" in options:
        raise MKGeneralException("You cannot specify --oid and --extraoid at the same time.")

    if not hostnames:
        raise MKBailOut("Please specify host names to walk on.")

    if not os.path.exists(cmk.utils.paths.snmpwalks_dir):
        os.makedirs(cmk.utils.paths.snmpwalks_dir)

    for hostname in hostnames:
        #TODO: What about SNMP management boards?
        snmp_config = create_snmp_host_config(hostname)

        try:
            _do_snmpwalk_on(snmp_config, options, cmk.utils.paths.snmpwalks_dir + "/" + hostname)
        except Exception as e:
            console.error("Error walking %s: %s\n" % (hostname, e))
            if cmk.utils.debug.enabled():
                raise
        cmk.base.cleanup.cleanup_globals()


def _do_snmpwalk_on(snmp_config, options, filename):
    # type: (SNMPHostConfig, SNMPWalkOptions, str) -> None
    console.verbose("%s:\n" % snmp_config.hostname)

    oids = oids_to_walk(options)

    with Path(filename).open("w", encoding="utf-8") as file:
        for rows in _execute_walks_for_dump(snmp_config, oids):
            for oid, value in rows:
                file.write("%s %s\n" % (oid, value))
            console.verbose("%d variables.\n" % len(rows))

    console.verbose("Wrote fetched data to %s%s%s.\n" % (tty.bold, filename, tty.normal))


def _execute_walks_for_dump(snmp_config, oids):
    # type: (SNMPHostConfig, List[OID]) -> Iterable[SNMPRowInfoForStoredWalk]
    for oid in oids:
        try:
            console.verbose("Walk on \"%s\"..." % oid)
            yield walk_for_export(snmp_config, oid)
        except Exception as e:
            console.error("Error: %s\n" % e)
            if cmk.utils.debug.enabled():
                raise


def oids_to_walk(options=None):
    # type: (Optional[SNMPWalkOptions]) -> List[OID]
    if options is None:
        options = {}

    oids = [
        ".1.3.6.1.2.1",  # SNMPv2-SMI::mib-2
        ".1.3.6.1.4.1"  # SNMPv2-SMI::enterprises
    ]

    if "oids" in options:
        oids = options["oids"]

    elif "extraoids" in options:
        oids += options["extraoids"]

    return sorted(oids, key=lambda x: list(map(int, x.strip(".").split("."))))


def do_snmpget(oid, hostnames):
    # type: (OID, List[HostName]) -> None
    assert hostnames
    for hostname in hostnames:
        #TODO what about SNMP management boards?
        snmp_config = create_snmp_host_config(hostname)
        backend = factory.backend(snmp_config)
        snmp_cache.initialize_single_oid_cache(snmp_config)

        value = get_single_oid(snmp_config, oid, backend=backend)
        sys.stdout.write("%s (%s): %r\n" % (hostname, snmp_config.ipaddress, value))
        cmk.base.cleanup.cleanup_globals()


cmk.base.cleanup.register_cleanup(snmp_cache.cleanup_host_caches)
if inline:
    cmk.base.cleanup.register_cleanup(inline.cleanup_inline_snmp_globals)