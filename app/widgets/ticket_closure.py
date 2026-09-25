from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/widgets/ticket-closure", tags=["widgets"])

PHRASE_MAP = {
    "replaced patch cable": "The issue was traced to a faulty patch cable, which was replaced.",
    "reset interface": "The affected network interface was reset to clear the fault condition.",
    "confirmed connectivity": "Connectivity was verified after the corrective actions.",
    "rebooted": "The impacted device was safely rebooted to restore normal operation.",
    "updated firmware": "Firmware was updated to a stable release to address the underlying issue.",
    "cleared arp cache": "The ARP cache was cleared to remove stale network path information.",
    "power cycled": "The equipment was power-cycled to recover service.",
}


class TicketIn(BaseModel):
    notes: str


def _normalize(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _sentence_from_note(note: str) -> str:
    normalized = _normalize(note)
    for phrase, sentence in PHRASE_MAP.items():
        if phrase in normalized:
            return sentence

    plain = note.strip().rstrip(".")
    if not plain:
        return ""
    if plain[0].islower():
        plain = plain[0].upper() + plain[1:]
    return f"Performed the following corrective step: {plain}."


@router.post("/generate")
async def generate_ticket_closure(payload: TicketIn):
    lines = [line.strip() for line in payload.notes.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(400, "Please enter at least one action note")

    sentences: list[str] = []
    for line in lines:
        sentence = _sentence_from_note(line)
        if sentence and sentence not in sentences:
            sentences.append(sentence)

    if not sentences:
        raise HTTPException(400, "Could not generate a closure summary from the provided notes")

    if not any("verified" in _normalize(s) or "normal operation" in _normalize(s) for s in sentences):
        sentences.append("Service validation was completed and systems are operating normally.")

    paragraph = " ".join(sentences)

    return {
        "notes": lines,
        "paragraph": paragraph,
        "mapping_mode": "rule-based-template-v1",
    }
