"""LLM-as-judge for the comparison tab.

Given N strategy results for the same query, asks Gemini to score each on
four criteria (1 = sehr gut, 5 = mangelhaft, deutsche Schulnoten) and pick a
winner. Built to be robust against malformed JSON output: we extract the
first {…} block, parse it, and fall back to neutral scores if anything trips."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable

from backend.llm.factory import get_chat_llm

log = logging.getLogger(__name__)

CRITERIA = ("korrektheit", "vollstaendigkeit", "quellenbezug", "fokussiertheit")

JUDGE_SYSTEM = (
    "Du bist ein neutraler, strenger Bewerter von RAG-Antworten. Du bekommst "
    "die ursprüngliche Frage, mehrere Antworten verschiedener Retrieval-"
    "Strategien und die jeweils genutzten Quellen. Bewerte jede Antwort "
    "objektiv nach deutschen Schulnoten: 1 = sehr gut, 2 = gut, "
    "3 = befriedigend, 4 = ausreichend, 5 = mangelhaft. "
    "Halluzinationen, vorgebliche aber nicht in den Quellen belegte Fakten "
    "und unbegründete Zitate kosten Punkte in 'korrektheit' und 'quellenbezug'."
)


@dataclass
class JudgeScores:
    korrektheit: int
    vollstaendigkeit: int
    quellenbezug: int
    fokussiertheit: int
    kommentar: str

    @property
    def average(self) -> float:
        return round(
            (self.korrektheit + self.vollstaendigkeit
             + self.quellenbezug + self.fokussiertheit) / 4.0,
            2,
        )

    def to_dict(self) -> dict:
        return {
            "korrektheit": self.korrektheit,
            "vollstaendigkeit": self.vollstaendigkeit,
            "quellenbezug": self.quellenbezug,
            "fokussiertheit": self.fokussiertheit,
            "kommentar": self.kommentar,
            "gesamtnote": self.average,
        }


@dataclass
class JudgeEvaluation:
    scores_by_strategy: dict[str, JudgeScores]
    winner: str  # 'ue1' | 'ue2' | 'ue3' | 'tie'
    begruendung: str
    judge_model: str
    raw_response: str

    def to_dict(self) -> dict:
        return {
            "scores": {s: sc.to_dict() for s, sc in self.scores_by_strategy.items()},
            "gewinner": self.winner,
            "begruendung": self.begruendung,
            "judge_model": self.judge_model,
        }


def _format_sources(sources: list[dict]) -> str:
    if not sources:
        return "(keine)"
    paths = []
    for s in sources:
        path = s.get("section_path") or "?"
        paths.append(path)
    # Compact: deduplicate by path
    seen: dict[str, int] = {}
    for p in paths:
        seen[p] = seen.get(p, 0) + 1
    return ", ".join(f"{p} (×{n})" if n > 1 else p for p, n in seen.items())


MAX_ANSWER_CHARS_FOR_JUDGE = 1500


def _build_judge_prompt(query: str, results: list[dict]) -> str:
    """``results`` items: {strategy, answer, sources}.

    Two tweaks vs. the naive schema-prompt:
    (1) cap each answer to MAX_ANSWER_CHARS_FOR_JUDGE chars — plenty for an
        LLM to assess content quality, well within reliable-JSON budget.
    (2) present a *concrete example* JSON skeleton with placeholder values
        rather than schema-with-type-hints; models tend to mirror examples
        but copy schemas verbatim (returning `int` as a literal string).
    """
    parts = ["# Frage", query, ""]
    for r in results:
        strat = r["strategy"]
        answer = (r.get("answer") or "").strip() or "(keine Antwort)"
        if len(answer) > MAX_ANSWER_CHARS_FOR_JUDGE:
            answer = answer[:MAX_ANSWER_CHARS_FOR_JUDGE] + " …[gekürzt]"
        parts.append(f"## Antwort {strat.upper()}")
        parts.append('"""')
        parts.append(answer)
        parts.append('"""')
        parts.append(f"Genutzte Sektionen: {_format_sources(r.get('sources') or [])}")
        parts.append("")

    strategy_keys = [r["strategy"] for r in results]
    example_scores = ", ".join(
        f'"{k}": {{"korrektheit": 3, "vollstaendigkeit": 3, '
        f'"quellenbezug": 3, "fokussiertheit": 3, "kommentar": "..."}}'
        for k in strategy_keys
    )
    winner_choices = ", ".join(f"'{k}'" for k in strategy_keys) + ", 'tie'"

    parts.append("# Aufgabe")
    parts.append(
        f"Bewerte JEDE Antwort auf vier Kriterien (1 sehr gut – 5 mangelhaft). "
        f"Wähle einen Gewinner aus: {winner_choices}. "
        f"'tie' nur bei echtem Gleichstand. Begründung 1–2 Sätze. "
        f"Antworte AUSSCHLIESSLICH mit gültigem JSON, keine Codefence, "
        f"keine Erklärung davor/danach. Beispiel-Struktur:"
    )
    parts.append("")
    parts.append('{ "scores": { ' + example_scores + ' }, '
                 f'"gewinner": "{strategy_keys[0]}", '
                 '"begruendung": "..." }')
    return "\n".join(parts)


def _extract_json(text: str) -> dict | None:
    """Pull a JSON object out of a model response that may have prose around
    it. Uses json.JSONDecoder.raw_decode() which parses the FIRST complete
    JSON value at a position and tells us where it ended — so we tolerate
    trailing text without falling for the greedy ``\\{.*\\}`` regex trap
    (which matches first ``{`` to last ``}`` and chokes on extra braces in
    explanation text)."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # Find the first '{' and try raw_decode from there; if that fails, try
    # the next '{', and so on. Limits to first few candidates so we don't
    # spend forever on adversarial input.
    decoder = json.JSONDecoder()
    for _ in range(5):
        idx = cleaned.find("{")
        if idx < 0:
            return None
        try:
            obj, _end = decoder.raw_decode(cleaned[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        # advance past this brace and try again
        cleaned = cleaned[idx + 1:]
    return None


def _clamp(value, lo: int = 1, hi: int = 5) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return 3
    return max(lo, min(hi, n))


def _parse_scores(raw: dict, strategy_keys: Iterable[str]) -> dict[str, JudgeScores]:
    scores: dict[str, JudgeScores] = {}
    section = raw.get("scores") or {}
    for k in strategy_keys:
        entry = section.get(k) or {}
        scores[k] = JudgeScores(
            korrektheit=_clamp(entry.get("korrektheit")),
            vollstaendigkeit=_clamp(entry.get("vollstaendigkeit")),
            quellenbezug=_clamp(entry.get("quellenbezug")),
            fokussiertheit=_clamp(entry.get("fokussiertheit")),
            kommentar=str(entry.get("kommentar") or "").strip()[:240],
        )
    return scores


def _neutral_evaluation(
    strategy_keys: list[str],
    raw_response: str,
    judge_model: str,
    error_note: str,
) -> JudgeEvaluation:
    scores = {
        k: JudgeScores(3, 3, 3, 3, error_note)
        for k in strategy_keys
    }
    return JudgeEvaluation(
        scores_by_strategy=scores,
        winner="tie",
        begruendung=f"Bewertung nicht möglich: {error_note}",
        judge_model=judge_model,
        raw_response=raw_response,
    )


def judge_answers(query: str, results: list[dict]) -> JudgeEvaluation:
    """Score N strategy answers and pick a winner.

    ``results`` items must have at least ``strategy``, ``answer``, ``sources``.
    Defaults to neutral scores if the LLM call or its output can't be parsed.
    """
    strategy_keys = [r["strategy"] for r in results]
    chat = get_chat_llm("gemini")
    judge_model = getattr(chat, "_model", "gemini")
    prompt = _build_judge_prompt(query, results)

    try:
        raw_response, _ = chat.generate(JUDGE_SYSTEM, prompt)
    except Exception as exc:  # noqa: BLE001
        log.warning("Judge LLM call failed: %s", exc)
        return _neutral_evaluation(strategy_keys, "", judge_model,
                                   f"LLM-Aufruf fehlgeschlagen: {exc}")

    obj = _extract_json(raw_response or "")
    if obj is None:
        log.info("Judge returned unparseable response: %r", (raw_response or "")[:240])
        return _neutral_evaluation(strategy_keys, raw_response or "", judge_model,
                                   "Antwort des Schiedsrichters war kein gültiges JSON.")

    scores = _parse_scores(obj, strategy_keys)
    winner = str(obj.get("gewinner") or "tie")
    if winner not in (*strategy_keys, "tie"):
        winner = "tie"
    begruendung = str(obj.get("begruendung") or "").strip()[:600]

    return JudgeEvaluation(
        scores_by_strategy=scores,
        winner=winner,
        begruendung=begruendung,
        judge_model=judge_model,
        raw_response=raw_response,
    )
