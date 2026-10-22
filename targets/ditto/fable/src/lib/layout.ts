export interface LayoutNode {
  id: string;
  x: number;
  y: number;
  depth: number;
}

export interface LayoutEdge {
  src: string;
  dst: string;
  kind: string;
}

interface LayoutInput {
  ids: string[];
  edges: LayoutEdge[];
  roots?: string[];
}

// Layered placement in a unit box: entry points on top, the datastore at the bottom, requests flow
// downwards along edges (src depends on dst). Longest-path depth, then barycenter ordering per layer.
export function layerGraph({ ids, edges, roots }: LayoutInput): Map<string, LayoutNode> {
  const set = new Set(ids);
  const out = new Map<string, string[]>();
  const inc = new Map<string, string[]>();
  for (const id of ids) {
    out.set(id, []);
    inc.set(id, []);
  }
  for (const e of edges) {
    if (!set.has(e.src) || !set.has(e.dst) || e.src === e.dst) continue;
    out.get(e.src)!.push(e.dst);
    inc.get(e.dst)!.push(e.src);
  }
  const start = (roots ?? []).filter((r) => set.has(r));
  const starts = start.length ? start : ids.filter((id) => inc.get(id)!.length === 0);
  const depth = new Map<string, number>();
  for (const id of ids) depth.set(id, 0);
  for (const s of starts) depth.set(s, 0);
  // relax up to n times; cycles are tolerated by the bound
  for (let round = 0; round < ids.length; round++) {
    let changed = false;
    for (const e of edges) {
      if (!set.has(e.src) || !set.has(e.dst) || e.src === e.dst) continue;
      const d = depth.get(e.src)! + 1;
      if (d > depth.get(e.dst)! && d < ids.length) {
        depth.set(e.dst, d);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const layers = new Map<number, string[]>();
  for (const id of ids) {
    const d = depth.get(id)!;
    if (!layers.has(d)) layers.set(d, []);
    layers.get(d)!.push(id);
  }
  const depths = [...layers.keys()].sort((a, b) => a - b);
  const pos = new Map<string, LayoutNode>();
  depths.forEach((d, li) => {
    const members = layers.get(d)!;
    const key = (id: string) => {
      const parents = inc.get(id)!.filter((p) => pos.has(p));
      if (!parents.length) return id;
      const avg = parents.reduce((a, p) => a + pos.get(p)!.x, 0) / parents.length;
      return `${avg.toFixed(4)}|${id}`;
    };
    members.sort((a, b) => (key(a) < key(b) ? -1 : 1));
    const y = depths.length === 1 ? 0.5 : li / (depths.length - 1);
    members.forEach((id, i) => {
      const x = members.length === 1 ? 0.5 : (i + 0.5) / members.length;
      pos.set(id, { id, x, y, depth: d });
    });
  });
  return pos;
}
