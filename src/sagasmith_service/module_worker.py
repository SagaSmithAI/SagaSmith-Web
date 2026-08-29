from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import socket
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from prometheus_client import Counter, start_http_server
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sagasmith_service.config import Settings, get_settings
from sagasmith_service.database import make_engine, make_session_factory
from sagasmith_service.integrations.agent import AgentResult, AgentRuntime, HttpAgentRuntime
from sagasmith_service.integrations.dnd_mcp import DndRuntime, StreamableHttpDndRuntime
from sagasmith_service.models import (
    AuditEvent,
    ModuleDecision,
    ModuleInstallation,
    ModuleProject,
    ModuleRun,
    ModuleSource,
    User,
    UserNotification,
    now_utc,
)
from sagasmith_service.observability import MODULE_BLOCKING_IO_SECONDS
from sagasmith_service.quota import QuotaExceededError, reserve, settle
from sagasmith_service.quota import release as release_quota
from sagasmith_service.realtime import install_transactional_outbox
from sagasmith_service.room_tool_policy import campaign_phase_and_revision
from sagasmith_service.storage import LocalPrivateStorage, S3PrivateStorage

logger = logging.getLogger("sagasmith_service.module_worker")
MODULE_RUNS = Counter(
    "sagasmith_module_runs_total",
    "Persistent Module Studio task outcomes",
    ["run_type", "status"],
)
MODULE_RECOVERIES = Counter(
    "sagasmith_module_run_recoveries_total", "Expired Module Studio task leases recovered"
)

RUNNING_PROJECT_STATUS = {
    "outline": "outlining",
    "generate": "generating",
    "review": "draft_review",
    "revise": "generating",
    "finalize": "finalizing",
    "install": "compiled",
}
_IoResult = TypeVar("_IoResult")


class BoundedBlockingIo:
    """Run storage/filesystem calls off-loop with process-local backpressure."""

    def __init__(self, concurrency: int) -> None:
        if concurrency < 1:
            raise ValueError("module worker IO concurrency must be positive")
        self._slots = asyncio.Semaphore(concurrency)

    async def run(
        self,
        operation: str,
        function: Callable[..., _IoResult],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> _IoResult:
        async with self._slots:
            started = asyncio.get_running_loop().time()
            status = "success"
            try:
                return await asyncio.to_thread(function, *args, **kwargs)
            except BaseException:
                status = "error"
                raise
            finally:
                MODULE_BLOCKING_IO_SECONDS.labels(
                    operation=operation,
                    status=status,
                ).observe(asyncio.get_running_loop().time() - started)


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "result" in value:
        return value["result"]
    return value


def _strict_json(content: str) -> dict[str, Any]:
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Hosted Agent did not return strict JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Hosted Agent result must be a JSON object")
    return value


def _source_text(storage: Any, source: ModuleSource | None) -> str:
    if source is None or Path(source.name).suffix.casefold() == ".pdf":
        return ""
    raw = storage.read_bytes(source.storage_key, max_bytes=2 * 1024 * 1024)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Module source must be UTF-8") from exc


def _job_view(receipt: dict[str, Any]) -> dict[str, Any]:
    result = _unwrap(receipt)
    return dict(result.get("job") or {}) if isinstance(result, dict) else {}


def _sync_draft(project: ModuleProject, receipt: dict[str, Any]) -> dict[str, Any]:
    result = _unwrap(receipt)
    if not isinstance(result, dict):
        return {}
    job = dict(result.get("job") or {})
    project.mcp_job_id = (
        str(result.get("job_id") or job.get("id") or project.mcp_job_id or "") or None
    )
    project.mcp_module_id = (
        str(result.get("module_id") or job.get("module_id") or project.mcp_module_id or "") or None
    )
    revision = job.get("revision")
    if isinstance(revision, int):
        project.mcp_draft_revision = revision
    project.mcp_draft_state = str(job.get("state") or project.mcp_draft_state or "") or None
    if isinstance(result.get("inspection"), dict):
        project.inspection = result["inspection"]
    if isinstance(result.get("validation"), dict):
        project.validation = result["validation"]
    return result


def _package_decisions_ready(decisions: dict[str, Any]) -> bool:
    """Check the current MCP finalization contract without inventing rulings."""

    manifest = decisions.get("manifest")
    if not isinstance(manifest, dict) or not str(manifest.get("title") or "").strip():
        return False
    if not isinstance(manifest.get("activation"), dict) or not isinstance(
        manifest.get("continuity"), dict
    ):
        return False
    profile = manifest.get("play_profile")
    if not isinstance(profile, dict):
        return False
    required = ("starting_level", "expected_end_level", "advancement", "pregenerated_characters")
    for key in required:
        value = profile.get(key)
        if not isinstance(value, dict) or not value.get("source_refs"):
            return False
    return True


class ModuleJobProcessor:
    """Persistent authoring worker; Agent decides meaning and MCP owns the draft."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        dnd_runtime: DndRuntime,
        agent_runtime: AgentRuntime,
        storage: Any,
        settings: Settings,
        *,
        worker_id: str | None = None,
        blocking_io: BoundedBlockingIo | None = None,
    ) -> None:
        self.factory = factory
        self.dnd = dnd_runtime
        self.agent = agent_runtime
        self.storage = storage
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}-{id(self)}"
        self.blocking_io = blocking_io or BoundedBlockingIo(
            settings.module_worker_io_concurrency
        )

    def recover_expired(self) -> int:
        now = now_utc()
        with self.factory() as session:
            rows = session.scalars(
                select(ModuleRun).where(
                    ModuleRun.status == "running",
                    ModuleRun.lease_expires_at < now,
                )
            ).all()
            for item in rows:
                item.status = "queued"
                item.lease_owner = None
                item.lease_expires_at = None
                item.available_at = now
                item.error = "Recovered after worker lease expiry"
            session.commit()
            if rows:
                MODULE_RECOVERIES.inc(len(rows))
            return len(rows)

    def claim(self) -> str | None:
        now = now_utc()
        with self.factory() as session:
            item = session.scalar(
                select(ModuleRun)
                .where(ModuleRun.status == "queued", ModuleRun.available_at <= now)
                .order_by(ModuleRun.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if item is None:
                return None
            item.status = "running"
            item.attempt += 1
            item.started_at = item.started_at or now
            item.lease_owner = self.worker_id
            item.lease_expires_at = now + timedelta(
                seconds=self.settings.module_worker_lease_seconds
            )
            project = session.get(ModuleProject, item.project_id)
            if project is not None:
                project.status = RUNNING_PROJECT_STATUS.get(item.run_type, project.status)
                project.last_error = ""
            session.commit()
            return item.id

    async def process_one(self) -> bool:
        self.recover_expired()
        run_id = self.claim()
        if run_id is None:
            return False
        try:
            with self.factory() as session:
                run = session.get(ModuleRun, run_id)
                if run is None:
                    return True
                if run.cancel_requested:
                    self._cancel(session, run)
                    session.commit()
                    return True
                run_type = run.run_type
            handler = getattr(self, f"_run_{run_type}", None)
            if handler is None:
                raise RuntimeError(f"Unsupported module run type: {run_type}")
            result = await handler(run_id)
            self._complete(run_id, result)
        except Exception as exc:
            logger.exception("module run failed run_id=%s", run_id)
            self._fail(run_id, exc)
        return True

    def _entities(self, session: Session, run_id: str) -> tuple[ModuleRun, ModuleProject, User]:
        run = session.get(ModuleRun, run_id)
        if run is None:
            raise RuntimeError("Module run disappeared")
        project = session.get(ModuleProject, run.project_id)
        user = session.get(User, run.requested_by_user_id)
        if project is None or user is None:
            raise RuntimeError("Module run references missing product state")
        if project.archived_at is not None:
            raise RuntimeError("Archived module projects cannot run")
        return run, project, user

    def _current_source(self, session: Session, project: ModuleProject) -> ModuleSource | None:
        if project.current_source_id:
            return session.get(ModuleSource, project.current_source_id)
        return session.scalar(
            select(ModuleSource)
            .where(ModuleSource.project_id == project.id)
            .order_by(ModuleSource.generation.desc())
        )

    async def _agent_json(
        self,
        run_id: str,
        *,
        task: str,
        contract: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        with self.factory() as session:
            run, project, user = self._entities(session, run_id)
            cached = dict(run.result or {}).get("agent_decision")
            if isinstance(cached, dict):
                return cached
            reservation_quantity = Decimal(self.settings.module_agent_reservation_tokens)
            if project.used_tokens + int(reservation_quantity) > project.budget_tokens:
                raise QuotaExceededError("Module project token budget is exhausted")
            reservation = reserve(
                session,
                user_id=user.id,
                campaign_id=project.authoring_campaign_id,
                metric="llm_tokens",
                quantity=reservation_quantity,
                idempotency_key=f"module-run:{run.id}",
                ttl_seconds=self.settings.module_worker_lease_seconds * 2,
            )
            run.reservation_id = reservation.id
            session.commit()
            session_id = (
                f"{project.authoring_campaign_id}:{user.id}:"
                f"module-{project.id[:12]}-{run.run_type}"
            )
            project_context = {
                "project_id": project.id,
                "title": project.title,
                "brief": project.brief,
                "edition": project.edition,
                "locale": project.locale,
                "version": project.version,
                "specification": project.specification,
                "outline": project.outline,
            }
            principal_id = user.principal_id
            campaign_id = project.authoring_campaign_id
            resource_owner_principal = f"user:{project.owner_user_id}"
            conversation_principal = f"module-project:{project.id}"
        runtime_state = await self.dnd.get_campaign(
            campaign_id=campaign_id,
            principal_id=principal_id,
        )
        _, base_revision = campaign_phase_and_revision("dnd5e", runtime_state)
        agent_idempotency_key = f"module-agent:{run_id}"
        authority_context = {
            "schema": "sagasmith.auth-context/v2",
            "target_service": "sagasmith-dnd-mcp",
            "caller_principal": "service:sagasmith-web",
            "workload_identity": "workload:module-worker",
            "requester_principal": principal_id,
            "resource_owner_principal": resource_owner_principal,
            "acting_host_principal": principal_id,
            "acting_character_id": "",
            "authorized_audience": "sagasmith-dnd-mcp",
            # The Agent may inspect the authoritative authoring campaign but cannot
            # mutate it. Web validates its JSON decision before calling module_draft.
            "allowed_operations": ["module_query"],
            "room_turn_id": run_id,
            "campaign_id": campaign_id,
            "system_id": "dnd5e",
            "base_revision": base_revision,
            "expires_at": (
                now_utc()
                + timedelta(seconds=self.settings.agent_delegation_ttl_seconds)
            ).isoformat(),
            "idempotency_key": agent_idempotency_key,
            "conversation_principal": conversation_principal,
            "tenant_id": "",
            "traceparent": "",
            "tracestate": "",
            "baggage": "",
        }
        prompt = (
            "Follow the installed sagasmith-modulegen Skill. This is a D&D module authoring "
            "decision, not a runtime narration. The Service will transport your explicit "
            "semantic decisions to the authoritative module_draft facade. Never invent a "
            "successful MCP receipt. Return strict JSON only, without Markdown fences.\n"
            f"Task: {task}\nContract: {contract}\n"
            + json.dumps({"project": project_context, "evidence": evidence}, ensure_ascii=False)
        )
        agent_result: AgentResult = await self.agent.complete(
            session_id=session_id,
            content=prompt,
            context={
                "campaign_id": campaign_id,
                "system_id": "dnd5e",
                "principal_id": principal_id,
                "campaign_role": "owner",
                "authority_context": authority_context,
            },
            idempotency_key=agent_idempotency_key,
        )
        decision = _strict_json(agent_result.content)
        with self.factory() as session:
            run, project, _ = self._entities(session, run_id)
            if run.cancel_requested or project.cancel_requested:
                raise RuntimeError("Module run was canceled")
            settle(
                session,
                reservation_id=str(run.reservation_id),
                quantity=Decimal(agent_result.total_tokens),
                idempotency_key=f"module-run-settle:{run.id}",
                unit="token",
                provider="hosted-agent",
                model=agent_result.model,
                request_id=agent_result.request_id,
            )
            run.prompt_tokens = agent_result.prompt_tokens
            run.completion_tokens = agent_result.completion_tokens
            run.model = agent_result.model
            run.upstream_request_id = agent_result.request_id
            run.result = {**dict(run.result or {}), "agent_decision": decision}
            project.used_tokens += agent_result.total_tokens
            session.commit()
        return decision

    async def _start_current_source(self, run_id: str) -> dict[str, Any]:
        with self.factory() as session:
            run, project, user = self._entities(session, run_id)
            if project.mcp_job_id:
                return {
                    "job_id": project.mcp_job_id,
                    "module_id": project.mcp_module_id,
                    "status": project.mcp_draft_state,
                }
            source = self._current_source(session, project)
            if source is None:
                raise RuntimeError("Upload or generate a module source first")
            campaign_id = project.authoring_campaign_id
            principal_id = user.principal_id
            source_key = f"module-project:{project.id}:source:{source.generation}"
            source_name = source.name
            storage_key = source.storage_key
            source_id = source.id
        source_text = await self.blocking_io.run(
            "source.read", _source_text, self.storage, source
        )
        payload: dict[str, Any] = {"title": project.title, "source_key": source_key}
        materialized: Path | None = None
        try:
            if source_text:
                payload.update({"name": source_name, "content": source_text})
            else:
                materialized = await self.blocking_io.run(
                    "source.materialize",
                    self.storage.materialize_source,
                    storage_key,
                    source_id,
                    source_name,
                )
                payload["source_path"] = str(materialized)
            receipt = await self.dnd.module_draft(
                campaign_id=campaign_id,
                action="start",
                payload=payload,
                principal_id=principal_id,
                idempotency_key=f"service:module:{run_id}:start:{source_id}",
            )
        finally:
            if materialized is not None:
                await self.blocking_io.run(
                    "source.cleanup", materialized.unlink, missing_ok=True
                )
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            _sync_draft(project, receipt)
            session.commit()
        return dict(_unwrap(receipt) or {})

    async def _draft_evidence(self, run_id: str) -> dict[str, Any]:
        with self.factory() as session:
            _, project, user = self._entities(session, run_id)
            if not project.mcp_job_id:
                return {}
            campaign_id = project.authoring_campaign_id
            job_id = project.mcp_job_id
            principal_id = user.principal_id
        chunks = await self.dnd.module_draft(
            campaign_id=campaign_id,
            action="evidence",
            payload={"job_id": job_id, "kind": "chunks", "limit": 100},
            principal_id=principal_id,
        )
        package = await self.dnd.module_draft(
            campaign_id=campaign_id,
            action="get",
            payload={"job_id": job_id, "view": "package"},
            principal_id=principal_id,
        )
        chunk_items = _unwrap(chunks)
        bounded_chunks = []
        for item in chunk_items if isinstance(chunk_items, list) else []:
            if not isinstance(item, dict):
                continue
            bounded_chunks.append(
                {
                    **item,
                    "content": str(item.get("content") or "")[:5000],
                }
            )
        return {"chunks": bounded_chunks, "package": _unwrap(package)}

    async def _store_generated_source(
        self, run_id: str, content: str, name: str
    ) -> ModuleSource:
        raw = content.encode("utf-8")
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            latest = session.scalar(
                select(ModuleSource.generation)
                .where(ModuleSource.project_id == project.id)
                .order_by(ModuleSource.generation.desc())
                .limit(1)
            )
            generation = int(latest or 0) + 1
            prior_sources = session.scalars(
                select(ModuleSource).where(ModuleSource.project_id == project.id)
            ).all()
            if any(item.rights_basis == "reference_only" for item in prior_sources):
                rights_basis = "reference_only"
            elif any(item.rights_basis == "open_licensed" for item in prior_sources):
                rights_basis = "open_licensed"
            else:
                rights_basis = "original"
            attributions = sorted(
                {item.attribution.strip() for item in prior_sources if item.attribution.strip()}
            )
            source_id = hashlib.sha256(f"{project.id}:{generation}".encode()).hexdigest()[:32]
            key = f"modules/{project.owner_user_id}/{project.id}/{source_id}.md"
            project_id = project.id
            prior_source_ids = [item.id for item in prior_sources]
        digest, size = await self.blocking_io.run(
            "source.write",
            self.storage.put,
            key,
            io.BytesIO(raw),
            max_bytes=self.settings.max_module_source_bytes,
            content_type="text/markdown; charset=utf-8",
        )
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            if project.id != project_id:
                raise RuntimeError("Module run changed projects while storing generated source")
            item = ModuleSource(
                id=source_id,
                project_id=project.id,
                generation=generation,
                source_type="generated" if generation == 1 else "revision",
                name=name,
                storage_key=key,
                sha256=digest,
                size_bytes=size,
                media_type="text/markdown",
                rights_basis=rights_basis,
                license_code="ARR",
                attribution="; ".join(attributions)[:2000],
                public_eligible=rights_basis != "reference_only",
                metadata_json={
                    "run_id": run_id,
                    "derived_from_source_ids": prior_source_ids,
                },
            )
            session.add(item)
            project.current_source_id = item.id
            project.mcp_job_id = None
            project.mcp_module_id = None
            project.mcp_draft_revision = None
            project.mcp_draft_state = None
            session.commit()
            return item

    async def _apply_package_decisions(
        self, run_id: str, decisions: dict[str, Any], note: str
    ) -> dict[str, Any]:
        allowed = {"catalogs", "dependencies", "manifest", "metadata", "narrative", "version"}
        normalized = {key: value for key, value in decisions.items() if key in allowed}
        if not normalized:
            return {}
        with self.factory() as session:
            _, project, user = self._entities(session, run_id)
            if not project.mcp_job_id:
                raise RuntimeError("Package decisions require an active draft")
            payload = {
                "job_id": project.mcp_job_id,
                "operation": "package",
                **normalized,
                "note": note,
            }
            revision = project.mcp_draft_revision
            campaign_id = project.authoring_campaign_id
            principal_id = user.principal_id
        receipt = await self.dnd.module_draft(
            campaign_id=campaign_id,
            action="edit",
            payload=payload,
            principal_id=principal_id,
            expected_revision=revision,
            idempotency_key=f"service:module:{run_id}:package",
        )
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            result = _sync_draft(project, receipt)
            project.package_decisions = {**dict(project.package_decisions or {}), **normalized}
            session.commit()
        return result

    async def _run_outline(self, run_id: str) -> dict[str, Any]:
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            source = self._current_source(session, project)
        evidence: dict[str, Any] = {}
        if source is not None:
            await self._start_current_source(run_id)
            evidence = await self._draft_evidence(run_id)
        decision = await self._agent_json(
            run_id,
            task="Design a playable outline grounded in the project brief and supplied evidence.",
            contract='{"outline":{"premise":string,"acts":array,"scenes":array,"endings":array,"risks":array},"summary":string}',
            evidence=evidence,
        )
        outline = decision.get("outline")
        if not isinstance(outline, dict):
            raise RuntimeError("Outline decision is missing outline")
        with self.factory() as session:
            run, project, user = self._entities(session, run_id)
            project.outline = outline
            project.outline_revision += 1
            project.status = "outline_ready"
            session.add(
                ModuleDecision(
                    project_id=project.id,
                    run_id=run.id,
                    actor_user_id=user.id,
                    decision_type="agent_outline",
                    project_revision=project.outline_revision,
                    payload=decision,
                )
            )
            session.commit()
        return {"outline": outline, "summary": str(decision.get("summary") or "")[:2000]}

    async def _run_generate(self, run_id: str) -> dict[str, Any]:
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            source = self._current_source(session, project)
        source_text = await self.blocking_io.run(
            "source.read", _source_text, self.storage, source
        )
        evidence = {"current_source": source_text[:1_000_000]} if source_text else {}
        decision = await self._agent_json(
            run_id,
            task="Generate the complete canonical D&D module source from the approved outline.",
            contract='{"canonical_source":string,"package_decisions":object,"summary":string}',
            evidence=evidence,
        )
        content = decision.get("canonical_source")
        if not isinstance(content, str) or len(content.strip()) < 200:
            raise RuntimeError("Generation decision is missing a complete canonical_source")
        await self._store_generated_source(run_id, content, "module.md")
        receipt = await self._start_current_source(run_id)
        decisions = decision.get("package_decisions")
        if isinstance(decisions, dict):
            await self._apply_package_decisions(run_id, decisions, "Agent generation decisions")
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            project.status = "draft_review"
            session.commit()
        return {"draft": receipt, "summary": str(decision.get("summary") or "")[:2000]}

    async def _run_review(self, run_id: str) -> dict[str, Any]:
        await self._start_current_source(run_id)
        evidence = await self._draft_evidence(run_id)
        decision = await self._agent_json(
            run_id,
            task="Review the mechanically imported draft against source evidence and playability.",
            contract=(
                '{"approved":boolean,"summary":string,"findings":'
                '[{"severity":string,"message":string,"evidence":object}],'
                '"package_decisions":{"version":string,"manifest":{"title":string,'
                '"classification":string,"compatibility":object,"activation":object,'
                '"continuity":object,"content_summary":object,"play_profile":'
                '{"starting_level":{"value":integer,"source_refs":array},'
                '"expected_end_level":{"value":integer,"source_refs":array},'
                '"advancement":{"modes":array,"recommended":string,"source_refs":array},'
                '"pregenerated_characters":{"available":boolean,'
                '"applicability":string,"source_refs":array}}}}}'
            ),
            evidence=evidence,
        )
        if not isinstance(decision.get("approved"), bool):
            raise RuntimeError("Review decision is missing approved")
        decisions = decision.get("package_decisions")
        if isinstance(decisions, dict) and decisions:
            await self._apply_package_decisions(run_id, decisions, "Agent evidence review")
        with self.factory() as session:
            run, project, user = self._entities(session, run_id)
            if decision["approved"] and not _package_decisions_ready(
                dict(project.package_decisions or {})
            ):
                raise RuntimeError(
                    "Approved review must include an evidence-sourced module play_profile"
                )
            project.review = {
                "approved": decision["approved"],
                "summary": str(decision.get("summary") or "")[:2000],
                "findings": list(decision.get("findings") or [])[:200],
                "reviewer": "hosted-agent",
                "run_id": run.id,
            }
            project.status = "ready_to_finalize" if decision["approved"] else "draft_review"
            session.add(
                ModuleDecision(
                    project_id=project.id,
                    run_id=run.id,
                    actor_user_id=user.id,
                    decision_type="agent_review",
                    project_revision=project.outline_revision,
                    payload=project.review,
                )
            )
            session.commit()
        return project.review

    async def _run_revise(self, run_id: str) -> dict[str, Any]:
        evidence = await self._draft_evidence(run_id)
        with self.factory() as session:
            run, project, _ = self._entities(session, run_id)
            instruction = str(run.input_payload.get("instruction") or "")
            next_version = str(run.input_payload.get("version") or "").strip()
        evidence["requested_revision"] = instruction
        decision = await self._agent_json(
            run_id,
            task=(
                "Revise the canonical module source using the review findings "
                "and author instruction."
            ),
            contract='{"canonical_source":string,"package_decisions":object,"summary":string}',
            evidence=evidence,
        )
        content = decision.get("canonical_source")
        if not isinstance(content, str) or len(content.strip()) < 200:
            raise RuntimeError("Revision decision is missing canonical_source")
        if next_version:
            with self.factory() as session:
                _, project, _ = self._entities(session, run_id)
                project.version = next_version
                project.published_release_id = None
                project.final_artifact = None
                project.final_pack_id = None
                project.final_checksum = None
                project.finalization = {}
                session.commit()
        await self._store_generated_source(run_id, content, "module-revision.md")
        receipt = await self._start_current_source(run_id)
        decisions = decision.get("package_decisions")
        if isinstance(decisions, dict):
            if next_version:
                decisions = {**decisions, "version": next_version}
            await self._apply_package_decisions(run_id, decisions, "Agent revision decisions")
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            project.status = "draft_review"
            project.review = {}
            session.commit()
        return {"draft": receipt, "summary": str(decision.get("summary") or "")[:2000]}

    async def _run_finalize(self, run_id: str) -> dict[str, Any]:
        with self.factory() as session:
            run, project, user = self._entities(session, run_id)
            if project.status not in {"finalizing", "ready_to_finalize"} or not project.review.get(
                "approved"
            ):
                raise RuntimeError("An approved Agent review is required before finalization")
            note = str(run.input_payload.get("note") or "")
            version = str(run.input_payload.get("version") or project.version)
            campaign_id = project.authoring_campaign_id
            job_id = project.mcp_job_id
            revision = project.mcp_draft_revision
            principal_id = user.principal_id
            # The MCP requires a stable Content Package identity at the
            # finalization boundary. Keep it opaque and project-scoped so two
            # tenants may freely choose the same public project slug.
            pack_id = project.final_pack_id or f"dnd5e.module.user.{project.id.replace('-', '')}"
        confirmation = await self._agent_json(
            run_id,
            task="Confirm finalization after checking the approved review and draft package.",
            contract='{"confirmed":true,"note":string}',
            evidence={"author_note": note, "version": version},
        )
        if confirmation.get("confirmed") is not True:
            raise RuntimeError("Hosted Agent did not confirm module finalization")
        confirmation_note = str(confirmation.get("note") or note).strip()
        if not confirmation_note:
            raise RuntimeError("Finalization confirmation note is required")
        receipt = await self.dnd.module_draft(
            campaign_id=campaign_id,
            action="finalize",
            payload={
                "job_id": job_id,
                "pack_id": pack_id,
                "version": version,
                "confirmation": {"confirmed": True, "note": confirmation_note[:2000]},
            },
            principal_id=principal_id,
            expected_revision=revision,
            idempotency_key=f"service:module:{run_id}:finalize",
        )
        result = dict(_unwrap(receipt) or {})
        summary = dict(result.get("summary") or {})
        artifact = str(result.get("artifact") or "")
        if not artifact:
            raise RuntimeError("MCP finalization returned no artifact")
        artifact_receipt = await self.dnd.get_content_artifact(
            campaign_id=campaign_id,
            artifact=artifact,
            principal_id=principal_id,
        )
        package = dict(_unwrap(artifact_receipt) or {})
        if str(package.get("id") or "") != pack_id:
            raise RuntimeError("Finalized artifact package id does not match the project")
        if str(package.get("version") or "") != version:
            raise RuntimeError("Finalized artifact version does not match the project")
        checksum = str(package.get("checksum") or "")
        if len(checksum) != 64:
            raise RuntimeError("Finalized artifact returned no SHA-256 checksum")
        with self.factory() as session:
            _, project, _ = self._entities(session, run_id)
            _sync_draft(project, receipt)
            project.version = version
            project.final_artifact = artifact
            project.final_pack_id = (
                str(summary.get("pack_id") or pack_id) or None
            )
            project.final_checksum = checksum or None
            project.finalization = {
                "confirmation": confirmation,
                "summary": summary,
                "package": {"id": pack_id, "version": version, "checksum": checksum},
                "run_id": run_id,
            }
            project.status = "compiled"
            session.commit()
        return {"artifact": artifact, "checksum": checksum, "summary": summary}

    async def _run_install(self, run_id: str) -> dict[str, Any]:
        with self.factory() as session:
            run, project, user = self._entities(session, run_id)
            campaign_id = str(run.input_payload.get("campaign_id") or "")
            activate = bool(run.input_payload.get("activate"))
            if not project.final_artifact:
                raise RuntimeError("Compile the module before installing it")
            artifact = project.final_artifact
            principal_id = user.principal_id
        receipt = await self.dnd.import_content_artifact(
            campaign_id=campaign_id,
            artifact=artifact,
            principal_id=principal_id,
            idempotency_key=f"service:module:{run_id}:install",
        )
        result = dict(_unwrap(receipt) or {})
        module_id = str(result.get("module_id") or dict(result.get("module") or {}).get("id") or "")
        activation: dict[str, Any] = {}
        if activate:
            if not module_id:
                raise RuntimeError("MCP import returned no module id for activation")
            activation = await self.dnd.activate_content_pack(
                campaign_id=campaign_id,
                kind="module",
                runtime_ref=module_id,
                pack_id=project.final_pack_id or module_id,
                version=project.version,
                principal_id=principal_id,
                idempotency_key=f"service:module:{run_id}:activate",
            )
        with self.factory() as session:
            run, project, user = self._entities(session, run_id)
            existing = session.scalar(
                select(ModuleInstallation).where(
                    ModuleInstallation.project_id == project.id,
                    ModuleInstallation.version == project.version,
                    ModuleInstallation.campaign_id == campaign_id,
                )
            )
            item = existing or ModuleInstallation(
                project_id=project.id,
                version=project.version,
                campaign_id=campaign_id,
                installed_by_user_id=user.id,
            )
            item.status = "active" if activate else "installed"
            item.runtime_module_id = module_id or None
            item.receipt = {"import": receipt, "activation": activation}
            session.add(item)
            session.flush()
            result_view = {"installation_id": item.id, "module_id": module_id, "active": activate}
            session.commit()
        return result_view

    def _complete(self, run_id: str, result: dict[str, Any]) -> None:
        with self.factory() as session:
            run, project, user = self._entities(session, run_id)
            run.status = "succeeded"
            run.result = {**dict(run.result or {}), **result}
            run.error = ""
            run.completed_at = now_utc()
            run.lease_owner = None
            run.lease_expires_at = None
            session.add(
                UserNotification(
                    user_id=user.id,
                    notification_type=f"module_{run.run_type}_completed",
                    title=f"{project.title}: {run.run_type} completed",
                    body="The Module Studio task finished successfully.",
                    action_url=f"/modules/{project.id}",
                )
            )
            session.add(
                AuditEvent(
                    actor_user_id=user.id,
                    action=f"module.run.{run.run_type}.complete",
                    subject_type="module_run",
                    subject_id=run.id,
                    details={"project_id": project.id, "attempt": run.attempt},
                )
            )
            session.commit()
            MODULE_RUNS.labels(run.run_type, "succeeded").inc()

    def _cancel(self, session: Session, run: ModuleRun) -> None:
        project = session.get(ModuleProject, run.project_id)
        run.status = "canceled"
        run.completed_at = now_utc()
        run.lease_owner = None
        run.lease_expires_at = None
        if project is not None:
            project.status = "canceled"
            project.cancel_requested = False

    def _fail(self, run_id: str, error: Exception) -> None:
        message = str(error)[:2000] or error.__class__.__name__
        with self.factory() as session:
            run = session.get(ModuleRun, run_id)
            if run is None:
                return
            project = session.get(ModuleProject, run.project_id)
            run.error = message
            run.lease_owner = None
            run.lease_expires_at = None
            if run.cancel_requested or (project is not None and project.cancel_requested):
                self._cancel(session, run)
                if run.reservation_id:
                    release_quota(session, run.reservation_id)
            elif run.attempt < run.max_attempts:
                run.status = "queued"
                run.available_at = now_utc() + timedelta(
                    seconds=self.settings.module_run_retry_seconds
                )
            else:
                run.status = "failed"
                run.completed_at = now_utc()
                if run.reservation_id:
                    release_quota(session, run.reservation_id)
                if project is not None:
                    project.status = "failed"
                    project.last_error = message
                session.add(
                    UserNotification(
                        user_id=run.requested_by_user_id,
                        notification_type="module_run_failed",
                        title="Module Studio task failed",
                        body=message,
                        action_url=f"/modules/{run.project_id}",
                    )
                )
            session.commit()
            if run.status in {"failed", "canceled"}:
                MODULE_RUNS.labels(run.run_type, run.status).inc()


async def _worker_loop(processor: ModuleJobProcessor, poll_seconds: float) -> None:
    while True:
        worked = await processor.process_one()
        if not worked:
            await asyncio.sleep(poll_seconds)


def _storage(settings: Settings) -> Any:
    if settings.storage_backend == "s3":
        return S3PrivateStorage(
            endpoint=settings.object_endpoint,
            bucket=settings.object_bucket,
            access_key=settings.object_access_key,
            secret_key=settings.object_secret_key.get_secret_value(),
            exchange_root=settings.exchange_dir,
        )
    return LocalPrivateStorage(settings.private_storage_dir, settings.exchange_dir)


async def run_workers(settings: Settings) -> None:
    start_http_server(settings.module_worker_metrics_port)
    install_transactional_outbox()
    factory = make_session_factory(make_engine(settings.database_url))
    dnd = StreamableHttpDndRuntime(
        settings.dnd_mcp_url,
        auth_context_secret=settings.auth_context_secret.get_secret_value(),
    )
    agent = HttpAgentRuntime(
        settings.agent_api_url,
        settings.agent_api_key.get_secret_value(),
        timeout_seconds=settings.agent_completion_timeout_seconds,
        boundary_mode=settings.agent_boundary_mode,
    )
    storage = await asyncio.to_thread(_storage, settings)
    blocking_io = BoundedBlockingIo(settings.module_worker_io_concurrency)
    processors = [
        ModuleJobProcessor(
            factory,
            dnd,
            agent,
            storage,
            settings,
            worker_id=f"module-{i}",
            blocking_io=blocking_io,
        )
        for i in range(settings.module_worker_concurrency)
    ]
    await asyncio.gather(
        *(_worker_loop(processor, settings.module_worker_poll_seconds) for processor in processors)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="SagaSmith persistent Module Studio worker")
    parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_workers(get_settings()))


if __name__ == "__main__":
    main()
