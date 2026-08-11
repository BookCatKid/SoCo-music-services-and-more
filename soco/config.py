"""This module contains configuration variables.

They may be set by your code as follows::

    from soco import config
    ...
    config.VARIABLE = value
"""

SOCO_CLASS = None
"""The class object to use when `SoCo` instances are created.

Specify the actual callable class object here, not a string. If `None`,
the default SoCo class will be used. Must be set before any instances are
created, or it will have unpredictable effects.
"""


CACHE_ENABLED = True
"""Is the cache enabled?

If `True` (the default), some caching of network requests will take place.

See also:
    The :mod:`soco.cache` module.
"""


EVENT_ADVERTISE_IP = None
"""The IP on which to advertise to Sonos.

The default of None means that the relevant IP address will be detected
automatically.

See also:
    The :mod:`soco.events_base` module.
"""

EVENT_LISTENER_IP = None
"""The IP on which the event listener listens.

The default of None means that the relevant IP address will be detected
automatically.

See also:
    The :mod:`soco.events_base` module.
"""


EVENT_LISTENER_PORT = 1400
"""The port on which the event listener listens.

The default is 1400. You must set this before subscribing to any events.

See also:
    The :mod:`soco.events_base` module.
"""

EVENTS_MODULE = None
"""The events module to be used by the :mod:`soco.services` module.

The default of None means the :mod:`soco.events` module will be used.

See also:
    The :mod:`soco.events` and :mod:`soco.events_twisted` modules.
"""

SERVICE_RETRY_ATTEMPTS = 3
"""The number of times a UPnP action is retried after a transient network error.

Only connection-level failures (timeouts, refused connections) are retried,
with a short backoff between attempts. SOAP faults and HTTP error responses
are never retried, because the action may already have been executed by the
player. Set to 1 to disable retries.

Note: a dropped reply after the player has executed a non-idempotent action
(such as ``AddURIToQueue``) could cause that action to be sent twice. Keep
this in mind when retrying mutating actions, and note that the worst-case
request time is now ``SERVICE_RETRY_ATTEMPTS * REQUEST_TIMEOUT``.
"""

REQUEST_TIMEOUT = 20.0
"""The timeout (in seconds) to be used when sending commands to a Sonos device.

A value for REQUEST_TIMEOUT *must* be set. It can be a float, an int, or None.
If set to 'None', calls can potentially wait indefinitely. (The default of 20.0s
is a long time for network operations, but it's been determined empirically to
be a reasonable upper limit for most circumstances.)

REQUEST_TIMEOUT can be set dynamically during program execution to adjust the
timeout at runtime. It can also be overridden for specific calls by using the
'timeout' kwarg in the relevant calling functions.
"""

ZGT_EVENT_FALLBACK = True
"""
For large Sonos systems (about 20+ players) the standard method of querying a
player for the Sonos Zone Group Topology will fail.

By default, SoCo will then fall back to using a method based on ZGT events. If
you wish to disable this behaviour, set 'ZGT_EVENT_FALLBACK' to 'False'. Your
code should then be prepared to catch `NotSupportedException` errors when
using functions that interrogate system state.
"""
