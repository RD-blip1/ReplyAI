// Renders comment text with invisible and direction-control characters made visible.
// An RLO (U+202E) would otherwise reverse the displayed text: the UI would show the very
// concealment the backend flags. ZWJ/ZWNJ are left alone (Devanagari, emoji sequences).
const NAMES: Record<number, string> = {
  0x200b: 'ZWSP', 0x200e: 'LRM', 0x200f: 'RLM', 0x202a: 'LRE', 0x202b: 'RLE', 0x202c: 'PDF',
  0x202d: 'LRO', 0x202e: 'RLO', 0x2060: 'WJ', 0x2066: 'LRI', 0x2067: 'RLI', 0x2068: 'FSI',
  0x2069: 'PDI', 0xfeff: 'BOM', 0x180e: 'MVS',
}

export function hiddenName(cp: number): string | null {
  if (NAMES[cp]) return NAMES[cp]
  if (cp >= 0xe0000 && cp <= 0xe007f) return 'TAG'
  return null
}

export type Part = { text: string } | { hidden: string }

export function revealHidden(text: string): Part[] {
  const parts: Part[] = []
  let run = ''
  for (const ch of text) {
    const name = hiddenName(ch.codePointAt(0) ?? 0)
    if (name) {
      if (run) parts.push({ text: run })
      run = ''
      const last = parts[parts.length - 1]
      // Collapse tag-character runs (ASCII smuggling) into one chip.
      if (name === 'TAG' && last && 'hidden' in last && last.hidden.startsWith('TAG')) {
        const n = Number(last.hidden.split('×')[1] ?? 1) + 1
        last.hidden = `TAG×${n}`
      } else {
        parts.push({ hidden: name })
      }
    } else {
      run += ch
    }
  }
  if (run) parts.push({ text: run })
  return parts
}

export function SafeText({ text }: { text: string }) {
  const parts = revealHidden(text)
  return (
    <span dir="auto" style={{ unicodeBidi: 'isolate' }}>
      {parts.map((p, i) => 'hidden' in p ? (
        <span key={i} title="Hidden character in the original comment"
          className="mx-0.5 inline-block rounded border border-warning/40 bg-warning/10 px-1 font-mono text-[10px] text-warning align-middle">
          {p.hidden}
        </span>
      ) : <span key={i}>{p.text}</span>)}
    </span>
  )
}
