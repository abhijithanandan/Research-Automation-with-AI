// Tiny line-level diff with no external deps.
//
// Computes the Longest-Common-Subsequence of two line arrays and emits a
// stream of operations: each line is either "kept" (in both), "removed" (only
// in the original), or "added" (only in the edited version). Used by the
// SynthesisReview override mode so the human can see exactly what they
// changed before approving (BRD §4.2 — manual override audit trail clarity).

export type DiffOp =
  | { type: "keep"; original: string; edited: string; lineA: number; lineB: number }
  | { type: "remove"; original: string; lineA: number }
  | { type: "add"; edited: string; lineB: number };

// Guard the O(n*m) LCS table against pathological inputs. The synthesis
// editor's payload is capped at ~256KB on the backend, so worst-case ~5000
// lines × 5000 lines = 25M dp cells × 8 bytes ≈ 200MB — enough to freeze
// the browser tab. Above this threshold we fall back to a coarse diff that
// emits one bulk "remove all original" + one bulk "add all edited" op,
// preserving the DiffOp output shape so callers don't change.
// Coderabbit PR #5 finding.
export const MAX_DP_CELLS = 1_000_000;

export function diffLines(original: string, edited: string): DiffOp[] {
  const a = original.split("\n");
  const b = edited.split("\n");
  const n = a.length;
  const m = b.length;

  if (n * m > MAX_DP_CELLS) {
    // eslint-disable-next-line no-console
    console.warn(
      `diffLines: input exceeds ${MAX_DP_CELLS} dp cells (${n}×${m}); using coarse fallback.`,
    );
    const ops: DiffOp[] = [];
    a.forEach((line, i) => ops.push({ type: "remove", original: line, lineA: i }));
    b.forEach((line, i) => ops.push({ type: "add", edited: line, lineB: i }));
    return ops;
  }

  // Build the LCS length table. dp[i][j] = LCS length of a[0..i) and b[0..j).
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      if (a[i - 1] === b[j - 1]) {
        dp[i]![j] = (dp[i - 1]?.[j - 1] ?? 0) + 1;
      } else {
        dp[i]![j] = Math.max(dp[i - 1]?.[j] ?? 0, dp[i]?.[j - 1] ?? 0);
      }
    }
  }

  // Walk the table backwards to recover the diff operations.
  const ops: DiffOp[] = [];
  let i = n;
  let j = m;
  while (i > 0 && j > 0) {
    if (a[i - 1] === b[j - 1]) {
      ops.push({
        type: "keep",
        original: a[i - 1] ?? "",
        edited: b[j - 1] ?? "",
        lineA: i - 1,
        lineB: j - 1,
      });
      i--;
      j--;
    } else if ((dp[i - 1]?.[j] ?? 0) >= (dp[i]?.[j - 1] ?? 0)) {
      ops.push({ type: "remove", original: a[i - 1] ?? "", lineA: i - 1 });
      i--;
    } else {
      ops.push({ type: "add", edited: b[j - 1] ?? "", lineB: j - 1 });
      j--;
    }
  }
  while (i > 0) {
    ops.push({ type: "remove", original: a[i - 1] ?? "", lineA: i - 1 });
    i--;
  }
  while (j > 0) {
    ops.push({ type: "add", edited: b[j - 1] ?? "", lineB: j - 1 });
    j--;
  }
  ops.reverse();
  return ops;
}

/** Quick summary for the diff header — how many lines changed. */
export interface DiffStats {
  added: number;
  removed: number;
  kept: number;
}

export function diffStats(ops: DiffOp[]): DiffStats {
  let added = 0;
  let removed = 0;
  let kept = 0;
  for (const op of ops) {
    if (op.type === "add") added++;
    else if (op.type === "remove") removed++;
    else kept++;
  }
  return { added, removed, kept };
}
