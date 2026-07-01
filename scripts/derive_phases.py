#!/usr/bin/env python3
"""Generic phase-deriver: auto-update kanban pipeline status from ground truth.

Usage:
    python3 .fleet/derive_phases.py
    python3 .fleet/derive_phases.py --repo-root /path/to/repo
"""
import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile


def _navigate(obj, dotpath):
    if not dotpath:
        return obj
    for key in dotpath.split('.'):
        if isinstance(obj, dict):
            if key not in obj:
                return None
            obj = obj[key]
        elif isinstance(obj, list):
            try:
                idx = int(key)
            except ValueError:
                return None
            if idx < 0 or idx >= len(obj):
                return None
            obj = obj[idx]
        else:
            return None
    return obj


class _ZeroDefault(dict):
    def __missing__(self, key):
        return 0


def _count_node(node):
    if isinstance(node, list):
        return len(node), {}
    if isinstance(node, dict):
        if not node:
            return 0, {}
        vals = list(node.values())
        if all(isinstance(v, list) for v in vals):
            total = sum(len(v) for v in vals)
            per_key = {k: len(v) for k, v in node.items()}
            return total, per_key
        return len(node), {}
    return 0, {}


def _compare(count, op, value):
    ops = {
        '>=': lambda a, b: a >= b,
        '>':  lambda a, b: a > b,
        '==': lambda a, b: a == b,
        '<':  lambda a, b: a < b,
        '<=': lambda a, b: a <= b,
    }
    fn = ops.get(op)
    return fn(count, value) if fn else False


def _eval_predicate(pred, repo_root, proc_alive):
    ptype = pred.get('type')
    if ptype == 'count':
        source = pred.get('source', '')
        fpath = os.path.join(repo_root, source)
        try:
            with open(fpath) as f:
                data = json.load(f)
        except Exception:
            return False, 0, {}
        node = _navigate(data, pred.get('path', ''))
        if node is None:
            return False, 0, {}
        count, per_key = _count_node(node)
        result = _compare(count, pred.get('op', '>='), pred.get('value', 0))
        return result, count, per_key
    elif ptype == 'process_alive':
        return proc_alive(pred.get('match', '')), 0, {}
    elif ptype == 'file_exists':
        fpath = os.path.join(repo_root, pred.get('source', ''))
        try:
            return (os.path.exists(fpath) and os.path.getsize(fpath) > 0), 0, {}
        except Exception:
            return False, 0, {}
    elif ptype == 'evaluative':
        # Judgment gate (e.g. a QG >= threshold). NEVER passes on passive watching:
        # it reads a score from a JUDGE-PRODUCED result file, so only a real
        # evaluation that WROTE that file (with a passing score) can flip the phase
        # to done. Missing file (no judge ran) -> not done. No rubber-stamping a
        # judgment phase by merely observing that some work happened.
        fpath = os.path.join(repo_root, pred.get('source', ''))
        try:
            with open(fpath) as f:
                data = json.load(f)
        except Exception:
            return False, 0, {}
        score = _navigate(data, pred.get('path', ''))
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return False, 0, {}
        return _compare(score, pred.get('op', '>='), pred.get('value', 0)), 0, {}
    return False, 0, {}


def derive_phases(meta, repo_root, proc_alive=None):
    """Pure function: returns a NEW meta dict with updated statuses.

    Does NOT mutate the input *meta*.
    """
    if proc_alive is None:
        proc_alive = _default_proc_alive
    result = copy.deepcopy(meta)
    for phase in result.get('phases', []):
        done_when = phase.get('done_when')
        active_when = phase.get('active_when')
        if done_when is None and active_when is None:
            continue
        done_result, done_count, done_per_key = False, 0, {}
        if done_when:
            try:
                done_result, done_count, done_per_key = _eval_predicate(
                    done_when, repo_root, proc_alive)
            except Exception:
                pass
        active_result = False
        if active_when:
            try:
                active_result, _, _ = _eval_predicate(
                    active_when, repo_root, proc_alive)
            except Exception:
                pass
        if done_result:
            phase['status'] = 'done'
        elif active_result:
            phase['status'] = 'active'
        elif done_when and done_when.get('type') == 'count' and done_count > 0:
            phase['status'] = 'active'
        gt = phase.get('gate_template')
        if gt and done_when and done_when.get('type') == 'count':
            fmt_vars = {'count': done_count, 'value': done_when.get('value', 0)}
            fmt_vars.update(done_per_key)
            try:
                phase['gate'] = gt.format_map(_ZeroDefault(fmt_vars))
            except (IndexError, ValueError):
                pass
    return result


def _default_proc_alive(match):
    try:
        r = subprocess.run(
            ['pgrep', '-f', match],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(
        description='Auto-update phases.json from ground truth')
    parser.add_argument('--repo-root', default=os.getcwd())
    parser.add_argument('--phases-file', default=None)
    args = parser.parse_args()

    repo_root = os.path.abspath(args.repo_root)
    phases_file = args.phases_file or os.path.join(
        repo_root, '.fleet', 'phases.json')
    phases_file = os.path.abspath(phases_file)

    with open(phases_file) as f:
        meta = json.load(f)

    updated = derive_phases(meta, repo_root)

    dir_path = os.path.dirname(phases_file)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as tmp:
            json.dump(updated, tmp, indent=2, ensure_ascii=False)
            tmp.write('\n')
        os.replace(tmp_path, phases_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


if __name__ == '__main__':
    main()
