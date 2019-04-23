from functools import wraps
import enum
from typing import Coroutine, TypeVar, Awaitable, Optional, Tuple, Any, List

from .._core.loop import __LOOP_STATE__, Interrupt
from .condition import Condition


RT = TypeVar('RT')


# enum.Flag is Py3.6+
class ActivityState(enum.Flag if hasattr(enum, 'Flag') else enum.IntEnum):
    """State of a :py:class:`~.Activity`"""
    #: created but not running yet
    CREATED = 2 ** 0
    #: being executed at the moment
    RUNNING = 2 ** 1
    #: finished due to cancellation
    CANCELLED = 2 ** 2
    #: finished due to an unhandled exception
    FAILED = 2 ** 3
    #: finished normally
    SUCCESS = 2 ** 4
    #: finished by any means
    FINISHED = CANCELLED | FAILED | SUCCESS


class ActivityCancelled(Exception):
    """An Activity has been cancelled"""
    __slots__ = ('subject',)

    def __init__(self, subject: 'Activity', *token):
        super().__init__(*token)
        self.subject = subject


class CancelActivity(Interrupt):
    """An Activity is being cancelled"""
    __slots__ = ('subject',)

    def __init__(self, subject: 'Activity', *token):
        super().__init__(*token)
        self.subject = subject

    @property
    def __transcript__(self) -> ActivityCancelled:
        result = ActivityCancelled(self.subject, *self.token)
        result.__cause__ = self
        return result


class ActivityExit(BaseException):
    ...


class Activity(Awaitable[RT]):
    """
    Concurrently running activity that allows multiple activities to await its completion

    A :py:class:`Activity` represents an activity that is concurrently run in a :py:class:`~.Scope`.
    This allows to store or pass an an :py:class:`Activity`, in order to check its progress.
    Other activities can ``await`` a :py:class:`Activity`,
    which returns any results or exceptions on completion, similar to a regular activity.

    .. code:: python3

        await my_activity()  # await a bare activity

        async with Scope() as scope:
            activity = scope.do(my_activity())
            await activity   # await a rich activity

    In contrast to a regular activity, it is possible to

    * :py:meth:`~.Activity.cancel` an :py:class:`Activity` before completion,
    * ``await`` the result of an :py:class:`Activity` multiple times,
      and
    * ``await`` that an is an :py:class:`Activity` is :py:meth:`~.Activity.cancel`.

    :note: This class should not be instantiated directly.
           Always use a :py:class:`~.Scope` to create it.
    """
    __slots__ = ('payload', '_result', '__runner__', '_cancellations', '_done')

    def __init__(self, payload: Coroutine[Any, Any, RT]):
        @wraps(payload)
        async def payload_wrapper():
            # check for a pre-run cancellation
            if self._result is not None:
                self.payload.close()
                return
            try:
                result = await self.payload
            except CancelActivity as err:
                assert err.subject is self, "activity %r received cancellation of %r" % (self, err.subject)
                self._result = None, err.__transcript__
            else:
                self._result = result, None
            for cancellation in self._cancellations:
                cancellation.revoke()
            self._done.__set_done__()
        self._cancellations = []  # type: List[CancelActivity]
        self._result = None  # type: Optional[Tuple[RT, BaseException]]
        self.payload = payload
        self._done = Done(self)
        self.__runner__ = payload_wrapper()  # type: Coroutine[Any, Any, RT]

    def __await__(self):
        yield from self._done.__await__()
        result, error = self._result
        if error is not None:
            raise error
        else:
            return result

    @property
    def done(self) -> 'Done':
        """
        :py:class:`~.Condition` whether the :py:class:`~.Activity` has stopped running.
        This includes completion, cancellation and failure.
        """
        return self._done

    @property
    def status(self) -> ActivityState:
        """The current status of this activity"""
        if self._result is not None:
            result, error = self._result
            if error is not None:
                return ActivityState.CANCELLED if isinstance(error, ActivityCancelled) else ActivityState.FAILED
            return ActivityState.SUCCESS
        # a stripped-down version of `inspect.getcoroutinestate`
        if self.__runner__.cr_frame.f_lasti == -1:
            return ActivityState.CREATED
        return ActivityState.RUNNING

    def __close__(self, reason=ActivityExit('activity closed')):
        """Close the underlying coroutine"""
        if self._result is None:
            self.__runner__.close()
            self._result = None, reason
            self._done.__set_done__()

    def cancel(self, *token) -> None:
        """Cancel this activity during the current time step"""
        if self._result is None:
            if self.status is ActivityState.CREATED:
                self._result = None, ActivityCancelled(self, *token)
                self._done.__set_done__()
            else:
                cancellation = CancelActivity(self, *token)
                self._cancellations.append(cancellation)
                cancellation.scheduled = True
                __LOOP_STATE__.LOOP.schedule(self.__runner__, signal=cancellation)

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
        self.__runner__.close()


class Done(Condition):
    """Whether a :py:class:`Activity` has stopped running"""
    __slots__ = ('_activity', '_value', '_inverse')

    def __init__(self, activity: Activity):
        super().__init__()
        self._activity = activity
        self._value = False
        self._inverse = NotDone(self)

    def __bool__(self):
        return self._value

    def __invert__(self):
        return self._inverse

    def __set_done__(self):
        """Set the boolean value of this condition"""
        assert not self._value
        self._value = True
        self.__trigger__()

    def __repr__(self):
        return '<%s for %r>' % (self.__class__.__name__, self._activity)


class NotDone(Condition):
    """Whether a :py:class:`Activity` has not stopped running"""
    __slots__ = ('_done',)

    def __init__(self, done: Done):
        super().__init__()
        self._done = done

    def __bool__(self):
        return not self._done

    def __invert__(self):
        return self._done

    def __repr__(self):
        return '<%s for %r>' % (self.__class__.__name__, self._done._activity)
