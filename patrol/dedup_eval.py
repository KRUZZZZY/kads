#!/usr/bin/env python3
"""Score a duplicate clustering of patrol findings against a hand-labelled gold set.

Gold files (research/reviews/luna-dedup-*/gold/{dev,test}-gold.json) partition every
finding of a few source files into groups ("one fix resolves both"). A "bridge" is a
report that describes two defects; its pairs with the second group are not scored.

With --multilabel, a bridge report's pairs with its primary group are unscored as well, so a
report naming two defects may join either group without penalty (only merges with unrelated
reports count against it).

A prediction maps finding id -> cluster label. Scores, over within-file pairs only:
pairwise precision / recall / F1, B-cubed precision / recall / F1, and the number of
gold findings the prediction also merges with findings outside their file.
"""
import argparse
import itertools
import json


def load_gold(path):
    g = json.load(open(path))
    label, files = {}, {}
    for f, groups in g["files"].items():
        files[f] = [i for grp in groups for i in grp]
        for k, grp in enumerate(groups):
            for i in grp:
                label[i] = (f, k)
    bridges = {int(b): set(v) for b, v in g.get("bridges", {}).items()}
    return label, files, bridges


def ignored(a, b, bridges, label=None, multilabel=False):
    if b in bridges.get(a, ()) or a in bridges.get(b, ()):
        return True
    if multilabel and label is not None:
        # A report that names two defects belongs to both groups: where it lands is not an error.
        return (a in bridges and label[a] == label[b]) or (b in bridges and label[a] == label[b])
    return False


def score(pred, gold_path, multilabel=False):
    label, files, bridges = load_gold(gold_path)
    tp = fp = fn = 0
    errors = []
    for f, ids in files.items():
        for a, b in itertools.combinations(sorted(ids), 2):
            if ignored(a, b, bridges, label, multilabel):
                continue
            same_gold = label[a] == label[b]
            same_pred = pred.get(a, ("solo", a)) == pred.get(b, ("solo", b))
            if same_gold and same_pred:
                tp += 1
            elif same_pred:
                fp += 1
                errors.append(("false_merge", a, b))
            elif same_gold:
                fn += 1
                errors.append(("missed", a, b))
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    # B-cubed over gold findings, within their own file.
    bp = br = 0.0
    n = 0
    for f, ids in files.items():
        for a in ids:
            pc = {b for b in ids if pred.get(b, ("solo", b)) == pred.get(a, ("solo", a))}
            gc = {b for b in ids if label[b] == label[a]}
            inter = len(pc & gc)
            bp += inter / len(pc)
            br += inter / len(gc)
            n += 1
    bp, br = bp / n, br / n
    bf = 2 * bp * br / (bp + br) if bp + br else 0.0
    gold_ids = set(label)
    members = {}
    for i, c in pred.items():
        members.setdefault(c, []).append(i)
    cross = sum(1 for a in gold_ids
                if any(b not in gold_ids or label[b][0] != label[a][0] for b in members.get(pred.get(a), []) if b != a))
    return {"pair_precision": round(p, 4), "pair_recall": round(r, 4), "pair_f1": round(f1, 4),
            "bcubed_precision": round(bp, 4), "bcubed_recall": round(br, 4), "bcubed_f1": round(bf, 4),
            "tp": tp, "fp": fp, "fn": fn, "cross_file_merged": cross, "errors": errors}


def pred_from_clusters(path):
    pred = {}
    for line in open(path):
        c = json.loads(line)
        for m in c["members"]:
            pred[m] = ("c", c["representative"])
    return pred


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--clusters", help="clusters.jsonl ({representative, members})")
    ap.add_argument("--pred", help="JSON {id: label}")
    ap.add_argument("--errors", type=int, default=0, help="print up to N errors")
    ap.add_argument("--multilabel", action="store_true", help="do not score a bridge report's pairs with either of its groups")
    a = ap.parse_args()
    pred = pred_from_clusters(a.clusters) if a.clusters else {int(k): v for k, v in json.load(open(a.pred)).items()}
    s = score(pred, a.gold, a.multilabel)
    errs = s.pop("errors")
    print(json.dumps(s))
    for e in errs[:a.errors]:
        print(*e)


if __name__ == "__main__":
    main()
