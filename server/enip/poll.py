#! /usr/bin/env python

# 
# Cpppo -- Communication Protocol Python Parser and Originator
# 
# Copyright (c) 2013, Hard Consulting Corporation.
# 
# Cpppo is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  See the LICENSE file at the top of the source tree.
# 
# Cpppo is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# 

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

try:
    from future_builtins import zip # Use Python 3 "lazy" zip
except ImportError:
    pass

__author__                      = "Perry Kundert"
__email__                       = "perry@hardconsulting.com"
__copyright__                   = "Copyright (c) 2016 Hard Consulting Corporation"
__license__                     = "Dual License: GPLv3 (or later) and Commercial (see LICENSE)"

__all__				= [
    'PARAMS', 'execute', 'loop', 'run', 'poll', 'main',
]

import argparse
import contextlib
import importlib
import json
import logging
import sys
import time
import traceback
import warnings

from ...automata import log_cfg
from ...misc import timer
from .main import address as enip_address
from .client import connector

# Default poll params list, used if None supplied to poll; change, if desired
PARAMS				= [
    'Output Current',
    'Motor Velocity',
]

def execute( via, params=None, pass_thru=None ):
    """Perform a single poll via the supplied enip.get_attribute.gateway instance, yielding the
    parameters and their polled values.  Supply params (a sequence of CIP ('<address>', '<type>')),
    as might be produced by the provided via's class' parameter_substitution method...

    By default, we'll look for the parameters in the module's PARAMS list, which must be recognized
    by the supplied via's parameter_substitutions method, if pass_thru is not Truthy (default: True).

    Yields tuples of each of the supplied params, with their polled values.

    """
    with contextlib.closing( via.read(
            via.parameter_substitution( params or PARAMS, pass_thru=pass_thru ))) as reader:
        for p,v in zip( params or PARAMS, reader ): # "lazy" zip
            yield p,v


def loop( via, cycle=None, last_poll=None, **kwds ):
    """Monitor the desired cycle time (default: 1.0 seconds), perform a poll, and return the start of
    the poll cycle, the number of seconds to delay 'til the next poll cycle, and the list of
    parameter,value pairs polled:

        1449583850.949138,4.35,[(<parameter>,<value>),...]

    If the poll fails, an Exception is raised (and the powerflex proxy's gateway is closed in
    preparation for future poll attempts).  It is expected that the caller will re-attempt, after an
    appropriate delay (eg. one or more cycles).

    Call repeatedly (after waiting for the designated delay seconds to pass), passing the returned
    start of poll cycle in the 'last_poll' parameter.

    """
    # Detect where we are in poll cycle, logging early/missed polls, and advance last_poll to the
    # start of the current poll cycle.  We retain cadence by only initializing last_poll to the
    # current timer() if this is the first poll; otherwise, we always advance by cycles.
    if not cycle:
        cycle			= 1.0
    init_poll			= timer()
    dt				= init_poll - last_poll
    if dt < cycle:
        # An early poll; maybe just an out-of-cycle refresh...  Don't advance poll cycles
        logging.info( "Premature poll at %7.3fs into %7.3fs poll cycle", dt, cycle )
    else:
        # We're into this poll cycle....
        missed			= dt // cycle
        if last_poll:
            if missed > 1:
                logging.normal( "Missed %3d polls, %7.3fs past %7.3fs poll cycle",
                                missed, dt-cycle, cycle )
            last_poll	       += cycle * missed
        else:
            last_poll		= init_poll

    # last_poll has been advanced to indicate the start of the poll cycle we're within
    logging.info( "Polling started   %7.3fs into %7.3fs poll cycle", init_poll - last_poll, cycle )

    # Perform poll.  Whatever code "reifies" the powerflex.read generator must catch exceptions and
    # tell the (failed) powerflex instance to close its gateway.  This prepares the proxy's gateway
    # for subsequent I/O attempts (if any).
    with via: # ensure via.close_gateway invoked on any Exception
        with contextlib.closing( execute( via, **kwds )) as executor:
            # PyPy compatibility; avoid deferred destruction of generators
            results		= list( executor )

    done_poll			= timer()
    duration			= done_poll - init_poll
    logging.info( "Polling finished  %7.3fs into %7.3fs poll cycle, taking %7.3fs (%5.1f TPS)",
                  done_poll - last_poll, cycle, duration, (1.0/duration) if duration else float('inf'))

    # Return this poll cycle time stamp, remaining time 'til next poll cycle (if any), and results
    return last_poll,max( 0, last_poll+cycle-done_poll ),results


def run( via, process, failure=None, backoff_min=None, backoff_multiplier=None, backoff_max=None,
         latency=None, **kwds ):
    """Perform polling loop 'til process.done (or forever), and process each poll result.

    On Exception, invoke the supplied poll failure method (if any), and apply exponential back-off
    between repeated attempts to run the polling loop.  The default backoff starts at the poll cycle
    (or 1.0) seconds, and defaults to increase up to 10 times that, at a default rate of 1.5x the
    current backoff.

    One or more instance of poll.run may be using the same 'via' EtherNet/IP CIP proxy instance;
    it is assumed that Thread blocking behaviour is performed within the I/O processing code to
    ensure that only one Thread is performing I/O.

    """
    if backoff_min is None:
        backoff_min		= kwds.get( 'cycle' )
        if backoff_min is None:
            backoff_min		= 1.0
    if backoff_max is None:
        backoff_max		= backoff_min * 10
    if backoff_multiplier is None:
        backoff_multiplier	= 1.5
    if latency is None:
        latency			= .5

    backoff			= None
    lst,dly			= 0,0
    beg				= timer()
    while not hasattr( process, 'done' ) or not process.done:
        # Await expiry of 'dly', checking flags at least every 'latency' seconds
        ela			= timer() - beg
        if ela < dly:
            time.sleep( min( latency, dly - ela ))
            continue
        # Perform a poll.loop and/or increase exponential back-off.
        try:
            lst,dly,res		= loop( via, last_poll=lst, **kwds )
            for p,v in res:
                process( p, v )
            backoff		= None # Signal a successfully completed poll!
        except Exception as exc:
            if backoff is None:
                backoff		= backoff_min
                logging.normal( "Polling failure: waiting %7.3fs; %s", backoff, exc )
            else:
                backoff		= min( backoff * backoff_multiplier, backoff_max )
                logging.detail(  "Polling backoff: waiting %7.3fs; %s", backoff, exc )
            dly			= backoff
            if failure is not None:
                failure( exc )
        beg			= timer()


def poll( proxy_class=None, address=None, depth=None, multiple=None, timeout=None,
          route_path=None, send_path=None, gateway_class=None, via=None,
          params=None, pass_thru=None, cycle=None, process=None, failure=None,
          backoff_min=None, backoff_multiplier=None, backoff_max=None, latency=None ):
    """Connect to the Device (eg. CompactLogix, MicroLogix, PowerFlex) using the supplied 'via', or an
    instance of the provided proxy_class (something derived from enip.get_attribute.proxy,
    probably), at the specified address (the default enip.address, if None), and run polls, process
    (printing, by default) the results.

    Creates a new proxy_class instance, if necessary; each poll.poll method thus uses a separate
    EtherNet/IP CIP connection.

    This method does little; it may be useful to take control over the creation and lifespan of the
    proxy instance yourself, and invoke run manually; see poll_example*.py.  For example, you can
    run multiple poll.run methods in separate Threads, all sharing the same proxy instance, to
    achieve polling of various CIP Attributes at differing rates, over the same EtherNet/IP CIP
    session.

    PENDING DEPRECATION

    We used to call the first positional parameter 'gateway_class'; this is confusing, because the
    proxy classes call their underlying instance of the cpppo.server.enip.client connector their
    'gateway'.  So, we changed the name to proxy_class, and retained an optional keyword parameter
    'gateway_class'.  We'll warn for now, and deprecate it at a later major version change.

    """
    if gateway_class is not None:
        warnings.warn(
            "cpppo.server.enip.poll poll( gateway_class=... ) is deprecated; use proxy_class=... instead",
            PendingDeprecationWarning )
        assert proxy_class is None, "Cannot specify both gateway_class and proxy_class"
        proxy_class		= gateway_class
    if proxy_class is not None:
        assert via is None, "Cannot specify both a proxy_class and a 'via' proxy instance"
    if address is None:
        address			= enip_address # cpppo.server.enip.address
    if process is None:
        process			= lambda p,v: print( "%15s: %r" % ( p, v ))
    if via is None:
        via			= proxy_class(
            host=address[0], port=address[1], depth=depth, multiple=multiple, timeout=timeout,
            send_path=send_path, route_path=route_path )
    run( via=via, process=process, failure=failure, backoff_min=backoff_min,
         backoff_multiplier=backoff_multiplier, backoff_max=backoff_max, latency=latency,
         cycle=cycle, params=params, pass_thru=pass_thru )


def main( argv=None ):
    ap				= argparse.ArgumentParser(
        description = "Poll Parameters from CIP Device via proxy gateway (AB PowerFlex 750, by default)",
        epilog = "" )

    ap.add_argument( '-v', '--verbose', default=0, action="count",
                     help="Display logging information." )
    ap.add_argument( '-a', '--address', default="%s:%s" % enip_address,
                     help="Address of EtherNet/IP CIP device to connect to (default: %s:%s)" % (
                         enip_address[0], enip_address[1] ))
    ap.add_argument( '-c', '--cycle', default=None,
                     help="Poll cycle (default: 1)" )
    ap.add_argument( '-t', '--timeout', default=None,
                     help="I/O timeout (default: 1)" )
    ap.add_argument( '--route-path',
                     default=None,
                     help="Route Path, in JSON (default: %r); 0/false to specify no/empty route_path" % (
                         str( json.dumps( connector.route_path_default ))))
    ap.add_argument( '--send-path',
                     default=None,
                     help="Send Path to UCMM (default: @6/1); Specify an empty string '' for no Send Path" )
    ap.add_argument( '-S', '--simple', action='store_true',
                     default=False,
                     help="Access a simple (non-routing) EtherNet/IP CIP device (eg. MicroLogix)")
    ap.add_argument( '-m', '--multiple', action='store_true',
                     help="Use Multiple Service Packet request targeting ~500 bytes (default: False)" )
    ap.add_argument( '-d', '--depth', default=None,
                     help="Pipeline requests to this depth (default: 2)" )
    ap.add_argument( '-g', '--gateway', default='ab.powerflex_750_series',
                     help="Proxy gateway module.class for positioning actuator (default: ab.powerflex_750_series" )
    ap.add_argument( '-p', '--pass-thru', action='store_true',
                     help="Allow unrecognized parameters as Tags, CIP addresses (default: False)" )
    ap.add_argument( 'parameter', nargs="*",
                     help="Parameters to read")

    args			= ap.parse_args()

    # Set up logging level (-v...) and --log <file>
    # Set up logging level (-v...) and --log <file>
    levelmap 			= {
        0: logging.WARNING,
        1: logging.NORMAL,
        2: logging.DETAIL,
        3: logging.INFO,
        4: logging.DEBUG,
        }
    log_cfg['level']		= ( levelmap[args.verbose] 
                                    if args.verbose in levelmap
                                    else logging.DEBUG )

    logging.basicConfig( **log_cfg ) # cpppo.log_cfg

    # Load the specified Gateway module.class, and ensure class is present
    mod,cls			= args.gateway.split( '.' )
    gateway_module		= importlib.import_module( '.'+mod, package='cpppo.server.enip' )
    proxy_class			= getattr( gateway_module, cls )

    # Deduce interface:port address to connect to, and correct types (default is address, above)
    address			= args.address.split( ':', 1 )
    assert 1 <= len( address ) <= 2, "Invalid --address [<interface>]:[<port>}: %s" % args.address
    address			= ( str( address[0] ) if address[0] else enip_address[0],
                                    int( address[1] ) if len( address ) > 1 and address[1] else enip_address[1] )

    multiple			= 500 if args.multiple else 0
    depth			= int( args.depth ) if args.depth is not None else None
    timeout			= float( args.timeout ) if args.timeout is not None else None
    cycle			= float( args.cycle ) if args.cycle is not None else None
    route_path			= json.loads( args.route_path ) if args.route_path \
                                      else [] if args.simple else None
    send_path			= args.send_path                if args.send_path \
                                      else '' if args.simple else None

    try:
        poll( proxy_class, address=address, depth=depth, multiple=multiple, timeout=timeout,
              params=args.parameter, pass_thru=args.pass_thru, cycle=cycle,
              route_path=route_path, send_path=send_path )
    except (KeyboardInterrupt, SystemExit) as exc:
        logging.info( "Terminated normally due to %s", exc )
        return 0
    except Exception as exc:
        logging.warning( "Terminated with Exception: %s\n%s", exc, traceback.format_exc() )
        return 1


if __name__ == "__main__":
    sys.exit( main() )
