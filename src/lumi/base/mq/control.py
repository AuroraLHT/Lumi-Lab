"""The control plane: start, stop, drain, config, state, shutdown.

The old implementation had a structural bug. `register_control_callback` was a
classmethod on the mixin, and every decorator was written
`@BaseControlMixin.register_control_callback("state")` -- so `cls` was always the
*base*, and all three server classes wrote into one shared dict. `config`, `state`
and `terminate` were each registered three times and the last definition won for
every server type. `start`/`stop` leaked onto servers that had no streaming to start.

The fix is not to write the decorator more carefully. A decorator in a class body
runs *before the class exists*, so it fundamentally cannot know which class it is
on. Mark the method, and assemble the table in `__init_subclass__`, where the class
does exist -- and where the result lands in `cls.__dict__` and cannot be shared.

Also fixed here: an unknown verb used to get *no reply at all*, so the caller sat
until its timeout expired. And nothing ever registered `"shutdown"` -- only
`"terminate"`, which took the wrong arguments and would have raised TypeError if it
had ever been reached. Remote shutdown has never worked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ControlResponse:
    body: bytes = b""
    headers: dict[str, Any] = field(default_factory=dict)
    succ: bool = True
    error_type: str = ""
    error_message: str = ""

    @classmethod
    def ok(cls, body: bytes = b"", **headers: Any) -> "ControlResponse":
        return cls(body=body, headers=headers)

    @classmethod
    def error(cls, error_type: str, message: str) -> "ControlResponse":
        return cls(succ=False, error_type=error_type, error_message=message)


def control(verb: str) -> Callable:
    """Mark a method as the handler for a control verb.

    Every handler has the same signature: `(self, body: bytes, headers: dict) ->
    ControlResponse`. Uniformity is the point -- the old `terminate_server(self)`
    took no body or headers but was called with both, and `config_server` passed
    `self` twice.
    """

    def deco(fn: Callable) -> Callable:
        fn.__control_verb__ = verb  # type: ignore[attr-defined]
        return fn

    return deco


class ControlPlane:
    #: verb -> method name. Rebuilt per subclass; never shared.
    _control_verbs: ClassVar[dict[str, str]] = {}

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        table: dict[str, str] = {}
        # Base-first, so a subclass overriding a verb wins.
        for klass in reversed(cls.__mro__):
            for attr, value in vars(klass).items():
                verb = getattr(value, "__control_verb__", None)
                if verb is not None:
                    table[verb] = attr
        cls._control_verbs = table

    @property
    def control_verbs(self) -> tuple[str, ...]:
        return tuple(sorted(self._control_verbs))

    async def dispatch_control(self, verb: str, body: bytes, headers: dict[str, Any]) -> ControlResponse:
        method = self._control_verbs.get(verb)
        if method is None:
            # Always answer. Silence here is what made shutdown look like a timeout
            # for however long this has been deployed.
            return ControlResponse.error(
                "UnknownControlVerb",
                f"{type(self).__name__} does not handle {verb!r}; "
                f"known verbs: {', '.join(self.control_verbs)}",
            )
        try:
            return await getattr(self, method)(body, headers)
        except Exception as exc:  # a broken handler must not kill the control consumer
            log.exception("control verb %r failed", verb)
            return ControlResponse.error(type(exc).__name__, str(exc))
