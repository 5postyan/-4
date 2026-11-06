"""Конечный автомат этапов + журнал + итоговый протокол."""
import json, hashlib, logging
from pathlib import Path
import pandas as pd
from . import checks as K

LOGGER = logging.getLogger("launch")

NAMES = {1: "История счётчиков", 2: "Данные в реальном времени", 3: "Управление", 4: "Финальные дашборды"}
FN = {1: K.stage1, 2: K.stage2, 3: K.stage3, 4: K.stage4}
STATUS_RU = {"not_started": "Не начат", "in_progress": "В работе", "blocked": "Заблокирован", "completed": "Завершён"}
BLOCKING = ("fail", "unavailable")


class Clock:
    """Демо-часы: детерминированное время событий журнала (воспроизводимость)."""
    def __init__(self, start="2026-11-06T08:00:00Z"): self.t = pd.Timestamp(start)
    def peek(self): return self.t.strftime("%Y-%m-%dT%H:%M:%SZ")
    def now(self): self.t += pd.Timedelta(minutes=1); return self.t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Launch:
    def __init__(self, object_id, rules=None, clock=None, ctx=None):
        self.oid, self.R = object_id, rules or K.load_rules()
        self.clock, self.ctx = clock or Clock(), ctx or {}
        self.journal, self.confirmed, self.prev_status = [], {}, {n: "not_started" for n in NAMES}
        self.hashes_seen, self.result = {}, None

    # --- журнал ---
    def log(self, event, actor, stage=None, **kw):
        e = {"seq": len(self.journal) + 1, "time": self.clock.now(), "check_point": self.R["check_point_utc"], "object": self.oid, "actor": actor,
             "event": event, "stage": stage, "rules_version": self.R["version"], **kw}
        self.journal.append(e)
        LOGGER.info("event=%s object=%s stage=%s actor=%s", event, self.oid, stage, actor)
        return e

    # --- вычисление ---
    def evaluate(self, I, actor="system", reason="проверка"):
        hs = I.hashes(self.R)
        self.ctx["dash_hash"] = hs[4]
        changed = [n for n in NAMES if self.hashes_seen.get(n) not in (None, hs[n])]
        if changed:
            lo = min(changed)
            for n in range(lo, 5):
                if n in self.confirmed:
                    self.log("confirmation_stale", actor, n, reason=f"входы этапа {lo} изменены — подтверждения этапа {n} неактуальны до перепроверки", inputs_hash=self.hashes_seen.get(n))
                    del self.confirmed[n]
        stages, prev_done = [], True
        for n in NAMES:
            deps = [m for m in range(1, n) if stages[m - 1]["status"] != "completed"]
            if n >= 3 and deps:
                cs = []
            else:
                cs = FN[n](I)
            for c in cs: c["evidence"].update(checked_at=self.clock.peek(), check_point=self.R["check_point_utc"], input_hash=hs[n])
            mand = [c for c in cs if c["mandatory"]]
            blk = [c for c in mand if c["status"] in BLOCKING]
            pend = [c for c in mand if c["status"] == "pending"]
            if blk: st = "blocked"
            elif not cs: st = "not_started"
            elif pend or deps: st = "in_progress"
            else: st = "completed"
            obst = [{"check": c["id"], "title": c["title"], **o} for c in blk for o in (c["obstacles"] or [{"reason": c["summary"]}])]
            nxt = [c["next_action"] for c in blk if c["next_action"]] or [c["next_action"] for c in pend if c["next_action"]]
            if not nxt and deps: nxt = [f"Завершить этап {deps[0]} «{NAMES[deps[0]]}»"]
            if not nxt and st == "not_started": nxt = ["Начать этап"]
            if deps and st != "blocked": nxt = [f"Сначала завершить этап {deps[0]}"] + nxt
            stages.append({"n": n, "name": NAMES[n], "status": st, "status_ru": STATUS_RU[st], "checks": cs, "obstacles": obst, "next_action": nxt[0] if nxt else None,
                           "unclosed_dependencies": [{"stage": m, "name": NAMES[m], "status": stages[m - 1]["status"]} for m in deps],
                           "responsible": self.R["responsible"]["stage_4_acceptance"] if n == 4 else self.R["responsible"]["stages_1_3"], "inputs_hash": hs[n]})
        for s in stages:
            n = s["n"]
            if s["status"] != self.prev_status[n]:
                self.log("stage_transition", actor, n, **{"from": self.prev_status[n], "to": s["status"]}, reason=reason, inputs_hash=hs[n],
                         blockers=[f"{o['check']}: {o.get('reason') or o.get('field')}" for o in s["obstacles"]][:6])
                self.prev_status[n] = s["status"]
            if s["status"] == "completed": self.confirmed[n] = hs[n]
        self.hashes_seen = {n: hs[n] for n in NAMES}
        ready = all(s["status"] == "completed" for s in stages)
        self.result = {"object_id": self.oid, "name": I.passport.get("name"), "rules_version": self.R["version"], "check_point": self.R["check_point_utc"],
                       "ready": ready, "stages": stages, "corrections": I.corrections, "missing_files": I.missing_files,
                       "input_errors": I.input_errors,
                       "stats": {"passed": sum(c["status"] == "pass" for s in stages for c in s["checks"]), "failed": sum(c["status"] in BLOCKING for s in stages for c in s["checks"])}}
        if ready and not any(e["event"] == "launch_ready" for e in self.journal[-3:]):
            self.log("launch_ready", actor, None, inputs_hash=hs[4], reason="все 4 этапа завершены, дашборды приняты")
        return self.result

    def request_complete(self, I, stage, actor="engineer"):
        """Запрос завершить этап. Отклоняется, если предыдущие не завершены; материалы сохраняются."""
        r = self.result or self.evaluate(I, actor)
        deps = [m for m in range(1, stage) if r["stages"][m - 1]["status"] != "completed"]
        if deps:
            self.log("transition_rejected", actor, stage, reason="нарушен порядок этапов", unclosed_dependencies=[f"этап {m} «{NAMES[m]}»: {r['stages'][m-1]['status_ru']}" for m in deps], materials_kept=True)
            return {"accepted": False, "unclosed_dependencies": [{"stage": m, "name": NAMES[m], "status": r["stages"][m - 1]["status"]} for m in deps], "materials_kept": True}
        st = r["stages"][stage - 1]["status"]
        return {"accepted": st == "completed", "status": st, "obstacles": r["stages"][stage - 1]["obstacles"]}

    def accept_dashboards(self, I, by, role="Ответственный"):
        I.accept = {"by": by, "role": role, "at": self.clock.now(), "dash_hash": None}
        # хэш приёмки = хэш конфигурации блоков + ACL + паспорта + состояния этапа 3 (без самой приёмки)
        self.ctx["dash_hash"] = I.hashes(self.R)[4]; I.accept["dash_hash"] = self.ctx["dash_hash"]
        self.log("manual_confirmation", by, 4, kind="dashboard_acceptance", role=role, inputs_hash=self.ctx["dash_hash"])

    def apply_correction(self, I, rule_id, description, actor, fn):
        """Согласованная коррекция входа (оригинал не меняется): журналируется, меняет хэш входов."""
        fn(I); I.corrections.append({"rule": rule_id, "description": description, "by": actor})
        self.log("input_corrected", actor, None, rule=rule_id, description=description)


def protocol_md(res, journal):
    L = [f"# Протокол запуска объекта {res['object_id']} ({res['name']})", "", f"Версия правил: **{res['rules_version']}** · точка проверки: **{res['check_point']}** · итог: **{'ГОТОВ К ЭКСПЛУАТАЦИИ' if res['ready'] else 'НЕ ГОТОВ'}**", ""]
    for s in res["stages"]:
        L += [f"## Этап {s['n']}. {s['name']} — {s['status_ru']}", f"Ответственный: {s['responsible']}", ""]
        for c in s["checks"]:
            L.append(f"- [{c['status'].upper()}] {c['id']} {c['title']} — {c['summary']} _(источник: {c['evidence'].get('source')}, правила {c['evidence'].get('rules')})_")
        if s["obstacles"]: L += ["", "Препятствия:"] + [f"  - {o['check']}: {o.get('reason') or o.get('field')}" for o in s["obstacles"][:10]]
        if s["next_action"]: L += ["", f"Следующее действие: {s['next_action']}"]
        L.append("")
    L += ["## Журнал", "", "| # | время | кто | событие | этап | детали |", "|---|---|---|---|---|---|"]
    for e in journal:
        d = e.get("reason") or e.get("description") or e.get("kind") or ""
        if e["event"] == "stage_transition": d = f"{e['from']} → {e['to']}" + (f" ({e['reason']})" if e.get("reason") else "")
        L.append(f"| {e['seq']} | {e['time']} | {e['actor']} | {e['event']} | {e.get('stage') or ''} | {d} |")
    return "\n".join(L)



