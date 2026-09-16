#!/usr/bin/env python3
"""
Частотность — обёртка над `wordstat_client` в структуры, удобные пакету.

Публичный API:
- frequency(phrase, region)     -> Frequency
- frequencies(phrases, ...)     -> list[Frequency]
- expansions(phrase, ...)       -> list[Expansion]
- latest_snapshot()             -> (путь, {фраза: частотность})

Снапшоты лежат в каталоге данных: `<data_dir>/snapshots/wordstat-*.jsonl`.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from . import wordstat_client as _client


def client():
    """Клиент Wordstat API. Функция оставлена ради совместимости вызовов."""
    return _client


@dataclass
class Expansion:
    phrase: str
    freq: int
    kind: str  # top | association


@dataclass
class Frequency:
    phrase: str
    region: str
    #: totalCount Wordstat — показов в месяц по фразе
    freq: int | None
    expansions: list[Expansion] = field(default_factory=list)
    error: str | None = None


def _num(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse(phrase: str, region: str, res: dict) -> Frequency:
    if "_error" in res:
        return Frequency(phrase=phrase, region=region, freq=None, error=str(res["_error"]))

    exps = [
        Expansion(phrase=r.get("phrase", ""), freq=_num(r.get("count")), kind="top")
        for r in (res.get("results") or [])
    ] + [
        Expansion(phrase=r.get("phrase", ""), freq=_num(r.get("count")), kind="association")
        for r in (res.get("associations") or [])
    ]
    return Frequency(
        phrase=phrase,
        region=region,
        freq=_num(res.get("totalCount")),
        expansions=exps,
    )


def frequency(phrase: str, region: str = "225", num: int = 200) -> Frequency:
    res = client().top_requests(phrase, regions=[region], num=num)
    return _parse(phrase, region, res)


def frequencies(
    phrases: list[str], region: str = "225", num: int = 50, delay: float = 0.4
) -> list[Frequency]:
    out = []
    for p in phrases:
        out.append(frequency(p, region=region, num=num))
        time.sleep(delay)
    return out


def expansions(
    phrase: str, region: str = "225", num: int = 200, min_freq: int = 0
) -> list[Expansion]:
    f = frequency(phrase, region=region, num=num)
    return [e for e in f.expansions if e.freq >= min_freq]


def latest_snapshot() -> tuple[Path | None, dict[str, int]]:
    """Последний снапшот на диске: путь и карта {фраза: частотность}."""
    snapshots = config.snapshots_dir()
    if not snapshots.exists():
        return None, {}
    files = sorted(snapshots.glob("wordstat-*.jsonl"))
    if not files:
        return None, {}

    path = files[-1]
    freqs: dict[str, int] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            res = row.get("response") or {}
            if "_error" in res:
                continue
            freqs[row.get("seed", "")] = _num(res.get("totalCount"))
    return path, freqs


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Частотность и расширения через Wordstat")
    p.add_argument("--phrase", action="append", help="фраза (можно повторять)")
    p.add_argument("--region", default="225", help="код региона, по умолчанию 225 (Россия)")
    p.add_argument("--num", type=int, default=200, help="сколько расширений запрашивать")
    p.add_argument("--min-freq", type=int, default=30, dest="min_freq")
    p.add_argument("--limit", type=int, default=30, help="сколько расширений показать")
    p.add_argument("--snapshot", action="store_true", help="показать последний снапшот на диске")
    args = p.parse_args(argv)

    if args.snapshot:
        path, freqs = latest_snapshot()
        if not path:
            print("Снапшотов нет.")
            return 1
        print(f"{path.name}: сидов {len(freqs)}")
        for phrase, freq in sorted(freqs.items(), key=lambda kv: -kv[1])[: args.limit]:
            print(f"  {freq:>8}  {phrase}")
        return 0

    if not args.phrase:
        p.error("нужен --phrase или --snapshot")

    for f in frequencies(args.phrase, region=args.region, num=args.num):
        if f.error:
            print(f"\n{f.phrase!r}: ОШИБКА — {f.error}", file=sys.stderr)
            continue
        print(f"\n{f.phrase!r} — частотность {f.freq}, регион {f.region}")
        picked = sorted([e for e in f.expansions if e.freq >= args.min_freq],
                        key=lambda e: -e.freq)[: args.limit]
        if not picked:
            print(f"  расширений с частотностью ≥ {args.min_freq} нет")
        for e in picked:
            print(f"  {e.freq:>8}  {e.phrase}  ({e.kind})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
