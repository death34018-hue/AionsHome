"""Periodic, low-priority continuity work for Visitor Lounge."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
import logging
import re
from typing import Protocol
import unicodedata

from visitor_lounge.database import Database, utc_now
from visitor_lounge.models import Message, Summary, SummaryJob
from visitor_lounge.prompts import PromptBuilder
from visitor_lounge.repository import BackgroundRepository, VisitorRepository


logger = logging.getLogger(__name__)


MAX_RAW_SUMMARY_CHARACTERS = 800


class InvalidSummary(ValueError):
    """Raised when generated summary text is unsafe or outside its contract."""


SUMMARY_BATCH_VISITOR_MESSAGES = 15


class SummaryScheduler(Protocol):
    """Narrow boundary implemented by the later single-slot lifecycle wiring."""

    async def run_low_priority(
        self, work: Callable[[], Awaitable[None]]
    ) -> bool: ...


SummaryGenerator = Callable[
    [SummaryJob, str], Awaitable[tuple[str, dict[str, int]]]
]
TaskWaiter = Callable[
    [set[asyncio.Task[object]], float],
    Awaitable[tuple[set[asyncio.Task[object]], set[asyncio.Task[object]]]],
]


async def _wait_tasks(
    tasks: set[asyncio.Task[object]], timeout: float
) -> tuple[set[asyncio.Task[object]], set[asyncio.Task[object]]]:
    return await asyncio.wait(tasks, timeout=timeout)


@dataclass(frozen=True)
class BackgroundTick:
    suspended: int
    summary_jobs: tuple[SummaryJob, ...]


@dataclass(frozen=True)
class _RunEpoch:
    stopped: asyncio.Event


_SENSITIVE_PATTERNS = (
    re.compile(
        r"(?i)(?:\bpassword\b|\bpasscode\b|密码|口令|验证码)"
        r"\s*(?:(?:是|为|[:=：])\s*)?[^\s，。；,;]+"
    ),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?i)(?<![A-Z0-9])(?:\d{15}|\d{17}[0-9X])(?![A-Z0-9])"),
    re.compile(r"(?i)(?:护照(?:号)?\s*(?:是|为|[:=：])?\s*)?[A-Z][0-9]{7,9}"),
    re.compile(r"(?<!\d)1[3-9]\d(?:[-\s]?\d){8}(?!\d)"),
    re.compile(
        r"(?<!\d)(?:\(0\d{2,3}\)[-\s]?|0\d{2,3}[-\s]?)\d{7,8}(?!\d)"
    ),
    re.compile(
        r"(?:微信(?:号)?|QQ(?:号)?|手机号|手机|座机|电话|联系方式|邮箱)"
        r"\s*(?:是|为|[:=：])?\s*[A-Za-z0-9_.@+-]{5,}"
    ),
    re.compile(
        r"(?:住址|地址)?"
        r"(?:[\u4e00-\u9fff]{2,12}(?:省|自治区))?"
        r"(?:[\u4e00-\u9fff]{2,12}市)?"
        r"(?:[\u4e00-\u9fff]{1,12}(?:区|县))?"
        r"(?:[\u4e00-\u9fff]{1,12}(?:乡|镇|街道))?"
        r"(?:[\u4e00-\u9fff]{1,12}村)?"
        r"[\u4e00-\u9fff]{1,12}(?:路|街|道|巷)"
        r"\d{1,6}号"
        r"(?:\d{1,6}(?:栋|幢))?"
        r"(?:\d{1,6}单元)?"
        r"(?:\d{1,6}室)?"
    ),
)

_HIERARCHICAL_ADDRESS_CANDIDATE = re.compile(
    r"(?:(?:住址|地址)|(?<![\u4e00-\u9fff0-9]))"
    r"(?P<body>[\u4e00-\u9fff0-9]{4,120}\d{1,6}号"
    r"(?:\d{1,6}(?:栋|幢))?"
    r"(?:\d{1,6}单元)?"
    r"(?:\d{1,6}室)?)"
)
_ADMINISTRATIVE_LEVEL = re.compile(
    r"(?P<name>[\u4e00-\u9fff]{2,12}?)(?:自治区|街道|省|市|区|县|乡|镇|村|屯)"
)
_ADMINISTRATIVE_SUFFIX_CHARACTERS = frozenset("自治区街道省市区县乡镇村屯组")

_INFERENCE_SUBJECT = (
    r"(?:访客|对方|双方|两人|二人|其|他|她|他们|她们|此人|"
    r"该访客|接待人|接待者|主人)"
)
_EMOTIONAL_CATEGORY = (
    r"(?:焦虑|抑郁|恐惧|紧张|偏执|沮丧|悲伤|难过|愤怒|孤独|绝望|"
    r"低落|心理脆弱|心理异常)"
)
_PERSONALITY_CATEGORY = (
    r"(?:内向|外向|回避|依赖|控制|强势|敏感|孤僻|偏执|自恋)"
)
_RELATIONSHIP_CATEGORY = (
    r"(?:恋人|情侣|伴侣|夫妻|朋友|好友|同事|亲密关系)"
)
_RELATIONSHIP_QUALITY = (
    r"(?:稳定|不稳定|健康|有问题|亲密|疏远|破裂|和睦|融洽|紧张|"
    r"可以判定)"
)
_REFLEXIVE_TARGET = r"(?:自己|本人|彼此|相互)"
_SELF_REPORT_TIME = r"(?:最近|近期|目前|现在|当时|一度)?"
_SELF_REPORT_DEGREE = r"(?:很|非常|比较|较为|有些|十分|略显|不太)?"
_SELF_REPORT_PREDICATE = (
    rf"{_SELF_REPORT_TIME}(?:"
    rf"(?:感到|感觉|觉得){_SELF_REPORT_DEGREE}{_EMOTIONAL_CATEGORY}"
    r"(?:症|病|障碍)?|"
    rf"(?:患有|有){_EMOTIONAL_CATEGORY}(?:症|病|障碍)?|"
    rf"情绪{_SELF_REPORT_DEGREE}{_EMOTIONAL_CATEGORY}|"
    rf"{_SELF_REPORT_DEGREE}{_EMOTIONAL_CATEGORY}(?:症|病|障碍)?|"
    rf"(?:性格|人格){_SELF_REPORT_DEGREE}{_PERSONALITY_CATEGORY}"
    r"(?:型人格)?|"
    rf"(?:是|为|属于){_RELATIONSHIP_CATEGORY}|"
    rf"(?:关系|感情|婚姻){_SELF_REPORT_DEGREE}{_RELATIONSHIP_QUALITY})"
)

_ATTRIBUTED_SELF_REPORT = re.compile(
    rf"{_INFERENCE_SUBJECT}(?:曾经|曾|也|还|明确|主动)?"
    rf"(?:表示|称|说|提到|自述|说明){_REFLEXIVE_TARGET}"
    rf"{_SELF_REPORT_PREDICATE}"
)

_PROHIBITED_INFERENCE_PATTERNS = (
    re.compile(r"(?:诊断为|可以诊断)"),
    re.compile(
        r"(?:有|患|患有|显得|看起来|看上去|似乎|像是|表现得|表现为|"
        r"觉得|感到|感觉|很|非常|比较|较为|有些|十分|略显|不太)"
        rf".{{0,8}}{_EMOTIONAL_CATEGORY}(?:症|病|障碍)?"
    ),
    re.compile(r"(?:属于)?[\u4e00-\u9fff]{0,8}型人格"),
    re.compile(rf"(?:性格|人格).{{0,5}}{_PERSONALITY_CATEGORY}"),
    re.compile(
        rf"(?:关系|感情|婚姻).{{0,6}}{_RELATIONSHIP_QUALITY}"
    ),
    re.compile(
        rf"{_INFERENCE_SUBJECT}.{{0,10}}{_EMOTIONAL_CATEGORY}"
        r"(?:症|病|障碍)?"
    ),
    re.compile(
        rf"{_INFERENCE_SUBJECT}.{{0,8}}(?:性格|人格).{{0,5}}"
        rf"{_PERSONALITY_CATEGORY}"
    ),
    re.compile(
        rf"{_INFERENCE_SUBJECT}.{{0,8}}{_PERSONALITY_CATEGORY}"
        r"(?:型人格)"
    ),
    re.compile(
        rf"{_INFERENCE_SUBJECT}.{{0,8}}(?:是|为|属于|像)?"
        rf".{{0,3}}{_RELATIONSHIP_CATEGORY}"
    ),
    re.compile(
        rf"{_INFERENCE_SUBJECT}.{{0,8}}(?:关系|感情|婚姻).{{0,6}}"
        rf"{_RELATIONSHIP_QUALITY}"
    ),
)

_LONG_VERBATIM_QUOTE = re.compile(
    r'(?:“[^”]{31,}”|"[^"]{31,}"|「[^」]{31,}」|『[^』]{31,}』)'
)
_CONTROL_MARKER = re.compile(
    r"<<\s*LOUNGE_ACTION\s*[:=][^>]*>>", re.IGNORECASE
)


def sanitize_summary(
    text: str, *, source_messages: Sequence[Message] = ()
) -> str:
    """Accept every non-empty model memory without content-based rejection."""
    if not isinstance(text, str):
        raise TypeError("summary must be text")
    memory = text.strip()
    if not memory:
        raise InvalidSummary("empty_summary")
    return memory


def _redact_hierarchical_address(match: re.Match[str]) -> str:
    body = match.group("body")
    level_count = sum(
        any(
            character not in _ADMINISTRATIVE_SUFFIX_CHARACTERS
            for character in level.group("name")
        )
        for level in _ADMINISTRATIVE_LEVEL.finditer(body)
    )
    return "[已隐去]" if level_count >= 2 else match.group(0)


def _normalize_overlap_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", "", normalized)


def _contains_prohibited_inference(text: str) -> bool:
    clauses = re.split(r"[，,。！？!?；;]+", text)
    for clause in clauses:
        if not clause:
            continue
        unmasked = _ATTRIBUTED_SELF_REPORT.sub("[明确归因]", clause)
        if any(
            pattern.search(unmasked)
            for pattern in _PROHIBITED_INFERENCE_PATTERNS
        ):
            return True
    return False


class BackgroundCoordinator:
    """Perform one isolated scan or manage a cancellable 30-second scan loop."""

    def __init__(
        self,
        *,
        database: Database,
        prompt_builder: PromptBuilder,
        scheduler: SummaryScheduler | None = None,
        summary_generator: SummaryGenerator | None = None,
        clock: Callable[[], datetime] = utc_now,
        interval_seconds: float = 1800,
        summary_timeout_seconds: float = 120,
        stop_cleanup_timeout_seconds: float = 1,
        task_waiter: TaskWaiter = _wait_tasks,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("background interval must be positive")
        if summary_timeout_seconds <= 0:
            raise ValueError("summary timeout must be positive")
        if stop_cleanup_timeout_seconds < 0:
            raise ValueError("stop cleanup timeout must not be negative")
        self.repository = BackgroundRepository(database)
        self.visitors = VisitorRepository(database)
        self.prompt_builder = prompt_builder
        self.scheduler = scheduler
        self.summary_generator = summary_generator
        self._clock = clock
        self._interval_seconds = interval_seconds
        self._summary_timeout_seconds = summary_timeout_seconds
        self._stop_cleanup_timeout_seconds = stop_cleanup_timeout_seconds
        self._task_waiter = task_waiter
        self._task: asyncio.Task[None] | None = None
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._pending_dispatch_tasks: set[asyncio.Task[None]] = set()
        self._lease_owner_tasks: set[asyncio.Task[None]] = set()
        self._generator_tasks: set[asyncio.Task[object]] = set()
        self._dispatching_job_ids: set[str] = set()
        self._epoch = _RunEpoch(asyncio.Event())

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def record_activity(self, visitor_id: str, at: datetime) -> None:
        self.repository.record_activity(visitor_id, at)

    def status(self, visitor_id: str) -> str:
        return self.visitors.visitor(visitor_id).status

    def last_summarized_message_id(self, visitor_id: str) -> str | None:
        return self.repository.last_summarized_message_id(visitor_id)

    def summary_prompt(self, job: SummaryJob) -> str:
        context = self.repository.summary_context(
            job.visitor_id, job.first_message_id, job.last_message_id
        )
        memories = self.repository.recent_summaries(job.visitor_id, limit=1)
        previous_memory = memories[0] if memories else None
        return self.prompt_builder.summary(context, previous_memory)

    def suspend_idle_visits(self, now: datetime) -> int:
        return self.repository.suspend_idle_visits(now, idle_minutes=30)

    def enqueue_due_summaries(self, now: datetime) -> list[SummaryJob]:
        jobs = self.repository.due_summary_jobs(now)
        for visitor_id in self.repository.summary_candidates(
            now,
            idle_minutes=20,
            minimum_new=SUMMARY_BATCH_VISITOR_MESSAGES,
        ):
            source = self.repository.next_unsummarized_visitor_messages(
                visitor_id, limit=SUMMARY_BATCH_VISITOR_MESSAGES
            )
            if len(source) != SUMMARY_BATCH_VISITOR_MESSAGES:
                continue
            jobs.append(
                self.repository.create_summary_job(
                    visitor_id,
                    source[0].id,
                    source[-1].id,
                    now,
                )
            )

        unique_jobs = list({job.id: job for job in jobs}.values())
        return unique_jobs

    def complete_summary(
        self,
        snapshot: SummaryJob,
        text: str,
        usage: dict[str, int],
        *,
        now: datetime | None = None,
    ) -> Summary:
        completed_at = now or self._clock()
        try:
            safe_text = self._validate_summary(snapshot, text)
        except InvalidSummary:
            self.fail_summary(snapshot, completed_at)
            raise
        return self.repository.complete_summary(
            snapshot, safe_text, usage, completed_at
        )

    def _validate_summary(self, snapshot: SummaryJob, text: str) -> str:
        source = self.repository.summary_context(
            snapshot.visitor_id,
            snapshot.first_message_id,
            snapshot.last_message_id,
        )
        return sanitize_summary(text, source_messages=source)

    def fail_summary(self, snapshot: SummaryJob, now: datetime) -> datetime | None:
        return self.repository.fail_summary(snapshot, now)

    def tick(self, now: datetime) -> BackgroundTick:
        return self._tick(now, self._epoch)

    def _tick(self, now: datetime, epoch: _RunEpoch) -> BackgroundTick:
        suspended = self.suspend_idle_visits(now)
        jobs = self.enqueue_due_summaries(now)
        self._schedule_dispatches(jobs, epoch)
        return BackgroundTick(suspended=suspended, summary_jobs=tuple(jobs))

    async def start(self) -> None:
        if self.running:
            return
        epoch = _RunEpoch(asyncio.Event())
        self._epoch = epoch
        self.repository.recover_abandoned_summary_attempts(self._clock())
        self.repository.recover_abandoned_summary_jobs(self._clock())
        self._task = asyncio.create_task(
            self._run(epoch), name="visitor-lounge-background"
        )

    async def stop(self) -> None:
        epoch = self._epoch
        epoch.stopped.set()
        task = self._task
        self._task = None
        tasks: list[asyncio.Task[object]] = []
        if task is not None:
            task.cancel()
            tasks.append(task)
        for dispatch in list(self._pending_dispatch_tasks):
            dispatch.cancel()
            tasks.append(dispatch)
        tasks.extend(self._lease_owner_tasks)
        for generator in list(self._generator_tasks):
            generator.cancel()
            tasks.append(generator)
        self.repository.interrupt_running_summary_attempts(self._clock())
        if tasks:
            await self._task_waiter(
                set(tasks), self._stop_cleanup_timeout_seconds
            )

    async def drain(self) -> None:
        """Wait until summary dispatches already started by a tick are settled."""
        while self._dispatch_tasks or self._generator_tasks:
            await asyncio.gather(
                *list(self._dispatch_tasks | self._generator_tasks),
                return_exceptions=True,
            )

    async def _run(self, epoch: _RunEpoch) -> None:
        while True:
            try:
                self._tick(self._clock(), epoch)
            except Exception as exc:
                logger.error(
                    "visitor lounge background tick failed; error_type=%s",
                    type(exc).__name__,
                )
            await asyncio.sleep(self._interval_seconds)

    def _schedule_dispatches(
        self, jobs: list[SummaryJob], epoch: _RunEpoch
    ) -> None:
        if (
            epoch.stopped.is_set()
            or self.scheduler is None
            or self.summary_generator is None
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for job in jobs:
            if job.id in self._dispatching_job_ids:
                continue
            self._dispatching_job_ids.add(job.id)
            task = loop.create_task(
                self._dispatch_summary(job, epoch),
                name=f"visitor-lounge-summary-{job.id}",
            )
            self._dispatch_tasks.add(task)
            self._pending_dispatch_tasks.add(task)
            task.add_done_callback(
                lambda done, job_id=job.id: self._dispatch_finished(job_id, done)
            )

    async def _dispatch_summary(self, job: SummaryJob, epoch: _RunEpoch) -> None:
        scheduler = self.scheduler
        generator = self.summary_generator
        if scheduler is None or generator is None:
            return

        async def work() -> None:
            current = asyncio.current_task()
            if current is None:
                raise RuntimeError("summary dispatch requires an asyncio task")
            self._pending_dispatch_tasks.discard(current)
            self._lease_owner_tasks.add(current)
            if epoch.stopped.is_set():
                return
            generation_task: asyncio.Task[object] | None = None
            attempt_id: str | None = None
            attempt_finished = False
            failure_recorded = False
            try:
                prompt = self.summary_prompt(job)
                attempt_id = self.repository.start_summary_attempt(
                    job.id, job.visitor_id, self._clock()
                )
                generation_task = asyncio.create_task(
                    generator(job, prompt),
                    name=f"visitor-lounge-summary-generator-{job.id}",
                )
                self._generator_tasks.add(generation_task)
                generation_task.add_done_callback(self._generator_finished)
                done, _ = await self._task_waiter(
                    {generation_task}, self._summary_timeout_seconds
                )
                if generation_task not in done:
                    generation_task.cancel()
                    self.repository.finish_summary_attempt(
                        attempt_id,
                        "timed_out",
                        self._clock(),
                        failure_reason="timeout",
                    )
                    attempt_finished = True
                    self.fail_summary(job, self._clock())
                    failure_recorded = True
                    late_result = await self._wait_for_actual_completion(
                        generation_task
                    )
                    self._supplement_late_usage(attempt_id, late_result)
                    return
                text, usage = generation_task.result()
                if epoch.stopped.is_set():
                    self.repository.supplement_summary_attempt_usage(
                        attempt_id, usage
                    )
                    return
                try:
                    safe_text = self._validate_summary(job, text)
                except InvalidSummary as exc:
                    self.repository.finish_summary_attempt(
                        attempt_id,
                        "failed",
                        self._clock(),
                        usage=usage,
                        failure_reason=str(exc),
                    )
                    attempt_finished = True
                    self.fail_summary(job, self._clock())
                    failure_recorded = True
                    return
                self.repository.finish_summary_attempt(
                    attempt_id, "completed", self._clock(), usage=usage
                )
                attempt_finished = True
                self.repository.complete_summary(
                    job, safe_text, usage, self._clock()
                )
            except InvalidSummary:
                return
            except asyncio.CancelledError:
                if attempt_id is not None and not attempt_finished:
                    self.repository.finish_summary_attempt(
                        attempt_id,
                        "interrupted",
                        self._clock(),
                        failure_reason="coordinator_stop",
                    )
                    attempt_finished = True
                late_result: object | None = None
                if generation_task is not None:
                    generation_task.cancel()
                    late_result = await self._wait_for_actual_completion(
                        generation_task
                    )
                if attempt_id is not None:
                    self._supplement_late_usage(attempt_id, late_result)
                if not failure_recorded and not epoch.stopped.is_set():
                    self.fail_summary(job, self._clock())
                raise
            except Exception as exc:
                if attempt_id is not None and not attempt_finished:
                    self.repository.finish_summary_attempt(
                        attempt_id,
                        "failed",
                        self._clock(),
                        failure_reason=type(exc).__name__,
                    )
                    attempt_finished = True
                if not failure_recorded and not epoch.stopped.is_set():
                    self.fail_summary(job, self._clock())

        try:
            await scheduler.run_low_priority(work)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.fail_summary(job, self._clock())

    def _dispatch_finished(
        self, job_id: str, task: asyncio.Task[None]
    ) -> None:
        self._dispatching_job_ids.discard(job_id)
        self._dispatch_tasks.discard(task)
        self._pending_dispatch_tasks.discard(task)
        self._lease_owner_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _generator_finished(self, task: asyncio.Task[object]) -> None:
        self._generator_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _supplement_late_usage(
        self, attempt_id: str, result: object | None
    ) -> None:
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[1], dict)
        ):
            self.repository.supplement_summary_attempt_usage(
                attempt_id, result[1]
            )

    @staticmethod
    async def _wait_for_actual_completion(
        task: asyncio.Task[object],
    ) -> object | None:
        """Keep the lease owner alive until a cancellation-resistant task ends."""
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                task.cancel()
            except Exception:
                break
        if task.cancelled():
            return None
        try:
            return task.result()
        except (asyncio.CancelledError, Exception):
            return None
