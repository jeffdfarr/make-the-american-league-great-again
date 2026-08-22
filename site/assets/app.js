/* MTALGA League HQ — shared helpers. No build step, no dependencies. */
const MT = {
  async data(...names) {
    const out = {};
    await Promise.all(names.map(async n => {
      const res = await fetch(`data/${n}.json`, { cache: "no-cache" });
      if (!res.ok) throw new Error(`failed to load ${n}.json`);
      out[n] = await res.json();
    }));
    return out;
  },

  pct(x, d = 3) { return x == null ? "—" : x.toFixed(d).replace(/^0\./, "."); },
  num(x, d = 0) { return x == null ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); },
  signed(x, d = 1) { return x == null ? "—" : (x >= 0 ? "+" : "") + x.toFixed(d); },
  rec(s) { return `${s.w}-${s.l}` + (s.t ? `-${s.t}` : ""); },

  initials(team) {
    const words = (team || "?").replace(/'/g, "").split(/\s+/).filter(w => !["the", "of"].includes(w.toLowerCase()));
    return ((words[0]?.[0] || "?") + (words[1]?.[0] || "")).toUpperCase();
  },

  teamCell(s) {
    const chip = `<span class="mono-chip">${MT.initials(s.team_name)}</span>`;
    const img = s.logo_url
      ? `<img class="logo" src="${s.logo_url}" alt="" loading="lazy"
             onerror="this.outerHTML='${chip.replace(/"/g, "&quot;")}'">`
      : chip;
    return `<td class="teamcell">${img}<span class="tstack"><b>${s.team_name || s.owner_name}</b><small>${s.owner_name || ""}</small></span></td>`;
  },

  oprCell(v, min, max) {
    if (v == null) return `<td class="num">—</td>`;
    const w = max > min ? 8 + 92 * (v - min) / (max - min) : 50;
    return `<td class="num"><span class="oprwrap"><i class="oprbar" style="width:${w.toFixed(0)}%"></i><b>${MT.pct(v).replace(".", v >= 1 ? "1." : "0.").slice(0, 5)}</b></span></td>`
      .replace(/<b>[^<]*<\/b>/, `<b>${v.toFixed(3)}</b>`);
  },

  divergeCell(v, maxAbs, d = 1) {
    if (v == null) return `<td class="num">—</td>`;
    const cls = v >= 0 ? "pos" : "neg";
    const w = Math.max(3, 50 * Math.abs(v) / (maxAbs || 1));
    const side = v < 0 ? "right:50%" : "left:50%";
    return `<td class="num ${cls}"><span class="mgwrap"><i class="mgbar ${cls}" style="${side};width:${w.toFixed(1)}%"></i><span class="mgval">${MT.signed(v, d)}</span></span></td>`;
  },

  badges(s) {
    const b = [];
    if (s.champion) b.push(`<span class="badge champ">CHAMP</span>`);
    else if (s.final_app) b.push(`<span class="badge final">FINAL</span>`);
    if (s.bye) b.push(`<span class="badge bye">BYE</span>`);
    return b.join(" ");
  },

  /* click-to-sort for any table: give <th> a data-key; rows carry data-<key> values */
  sortable(table) {
    table.querySelectorAll("thead th[data-key]").forEach(th => {
      th.addEventListener("click", () => {
        const key = th.dataset.key;
        const dir = th.dataset.sorted === "desc" ? "asc" : "desc";
        table.querySelectorAll("thead th").forEach(o => delete o.dataset.sorted);
        th.dataset.sorted = dir;
        const body = table.querySelector("tbody");
        const rows = [...body.querySelectorAll("tr")];
        rows.sort((a, b) => {
          const av = parseFloat(a.dataset[key]), bv = parseFloat(b.dataset[key]);
          const cmp = isNaN(av) || isNaN(bv)
            ? String(a.dataset[key]).localeCompare(String(b.dataset[key]))
            : av - bv;
          return dir === "desc" ? -cmp : cmp;
        });
        rows.forEach(r => body.appendChild(r));
        body.querySelectorAll("tr").forEach((r, i) => {
          const rank = r.querySelector(".rank"); if (rank) rank.textContent = i + 1;
        });
      });
    });
  },

  footer(meta) {
    const el = document.querySelector("footer");
    if (el && meta) el.innerHTML =
      `MTALGA League HQ &middot; data synced ${meta.generated ?? ""} &middot; ` +
      `OPR = (6&middot;PPG + 2&middot;(high+low) + 2&middot;(200&middot;win%)) / 10, normalized to league average by season`;
  },
};
