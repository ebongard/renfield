// remark plugin: turn `[[Target]]` in note bodies into link nodes so
// react-markdown renders them as clickable wiki-links. Operates on the mdast
// `text` nodes only (a self-contained walk — no extra dep), so `[[...]]` inside
// inline code / code fences is left verbatim (those are `inlineCode`/`code`
// leaf nodes, never visited here). The target title is carried in a
// `#wikilink:<title>` href that NoteMarkdown's custom `a` renderer intercepts.
//
// See docs/design/notes-atom.md (4B.3). Mirrors note_links.parse_links' regex.

// [[Target]] — no nested brackets; inner text is the target note title.
const WIKILINK = /\[\[([^\][]+)\]\]/g;

export const WIKILINK_HREF_PREFIX = '#wikilink:';

interface MdNode {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
}

function splitTextNode(value: string): MdNode[] {
  const parts: MdNode[] = [];
  let last = 0;
  for (const m of value.matchAll(WIKILINK)) {
    const idx = m.index ?? 0;
    if (idx > last) parts.push({ type: 'text', value: value.slice(last, idx) });
    const title = m[1].trim();
    parts.push({
      type: 'link',
      url: `${WIKILINK_HREF_PREFIX}${encodeURIComponent(title)}`,
      children: [{ type: 'text', value: title }],
    });
    last = idx + m[0].length;
  }
  if (last < value.length) parts.push({ type: 'text', value: value.slice(last) });
  return parts;
}

// Node types we must NOT turn a `[[x]]` inside into a link: existing links
// (a link inside a link is invalid mdast → nested <a>) and their reference form.
const SKIP_TYPES = new Set(['link', 'linkReference']);

function walk(node: MdNode): void {
  if (!node.children) return;
  const out: MdNode[] = [];
  for (const child of node.children) {
    if (child.type === 'text' && child.value && child.value.includes('[[')) {
      out.push(...splitTextNode(child.value));
    } else {
      // Recurse into containers, but never into an existing link (would nest).
      if (!SKIP_TYPES.has(child.type)) walk(child);
      out.push(child);
    }
  }
  node.children = out;
}

export default function remarkWikilink() {
  return (tree: MdNode): void => walk(tree);
}
