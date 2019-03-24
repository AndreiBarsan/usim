from functools import wraps
import enum
from typing import Coroutine, TypeVar, Generic, Optional, Tuple, Any, List

from .._core.loop import __LOOP_STATE__, Interrupt
from .condition import Condition


RT = TypeVar('RT')


class ActivityState(enum.Flag):
    """State of a :py:class:`~.Activity`"""
    #: created but not running yet
    CREATED = enum.auto()
    #: being executed at the moment
    RUNNING = enum.auto()
    #: finished due to cancellation
    CANCELLED = enum.auto()
    #: finished due to an unhandled exception
    FAILED = enum.auto()
    #: finished normally
    SUCCESS = enum.auto()
    #: finished by any means
    FINISHED = CANCELLED | FAILED | SUCCESS


class ActivityCancelled(Interrupt):
    __slots__ = ('subject',)

    def __init__(self, subject: 'Activity', *token):
        super().__init__(*token)
        self.subject = subject


class ActivityExit(BaseException):
    ...


class Activity(Condition, Generic[RT]):
    """
    Active coroutine that allows others to listen for its completion

    :note: Simulation code should never instantiate this class directly.
    """
    __slots__ = ('payload', '_result', '_execution', '_cancellations')

    def __init__(self, payload: Coroutine[Any, Any, RT]):
        @wraps(payload)
        async def payload_wrapper():
            # check for a pre-run cancellation
            if self._result is not None:
                self.payload.close()
                return
            try:
                result = await self.payload
            except ActivityCancelled as err:
                if err.subject is not self:
                    raise RuntimeError(
                        "activity %r failed to handle cancellation of %r" % (self, err.subject)
                    ) from err
                self._result = None, err
            else:
                self._result = result, None
            for cancellation in self._cancellations:
                cancellation.revoke()
            self.__trigger__()
        super().__init__()
        self._cancellations = []  # type: List[ActivityCancelled]
        self._result = None  # type: Optional[Tuple[RT, BaseException]]
        self.payload = payload
        self._execution = payload_wrapper()

    @property
    async def result(self) -> RT:
        """
        Wait for the completion of this :py:class:`Activity` and return its result

        :returns: the result of the activity
        :raises: :py:exc:`CancelActivity` if the activity was cancelled
        """
        await self
        result, error = self._result
        if error is not None:
            raise error
        else:
            return result

    @property
    def status(self) -> ActivityState:
        """The current status of this activity"""
        if self._result is not None:
            result, error = self._result
            if error is not None:
                return ActivityState.CANCELLED if isinstance(error, ActivityCancelled) else ActivityState.FAILED
            return ActivityState.SUCCESS
        # a stripped-down version of `inspect.getcoroutinestate`
        if self._execution.cr_frame.f_lasti == -1:
            return ActivityState.CREATED
        return ActivityState.RUNNING

    def __bool__(self):
        return self._result is not None

    def __invert__(self):
        return NotDone(self)

    def __runner__(self):
        return self._execution

    def __close__(self, reason=ActivityExit('activity closed')):
        """Close the underlying coroutine"""
        if self._result is None:
            self._execution.close()
            self._result = None, reason

    def cancel(self, *token) -> None:
        """Cancel this activity during the current time step"""
        if self._result is None:
            cancellation = ActivityCancelled(self, 'cancel activity', id(self)) \
                if not token else ActivityCancelled(self, *token)
            if self.status is ActivityState.CREATED:
                self._result = None, cancellation
                self.__trigger__()
            else:
                self._cancellations.append(cancellation)
                cancellation.scheduled = True
                __LOOP_STATE__.LOOP.schedule(self._execution, signal=cancellation)

    def __repr__(self):
        return '<%s of %s (%s)>' % (
            self.__class__.__name__, self.payload,
            'outstanding' if not self else (
                'result={!r}'.format(self._result[0])
                if self._result[1] is None
                else
                'signal={!r}'.format(self._result[1])
            ),
        )

    def __del__(self):
        # Since an Activity is only meant for use in a controlled
        # fashion, going out of scope unexpectedly means there is
        # a bug/error somewhere. This should be accompanied by an
        # error message or traceback.
        # In order not to detract with auxiliary, useless resource
        # warnings, we clean up silently to hide our abstraction.
        self._execution.close()


class NotDone(Condition):
    def __init__(self, activity: Activity):
        super().__init__()
        self.activity = activity

    def __bool__(self):
        return not self.activity

    def __invert__(self):
        return self.activity

    def __repr__(self):
        return '<%s for %r>' % (self.__class__.__name__, self.activity)
