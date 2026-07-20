import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkWikilink, { WIKILINK_HREF_PREFIX } from '../utils/remarkWikilink';

/**
 * Renders a note body as markdown (react-markdown → real React elements, so the
 * strict CSP holds — no innerHTML, no eval). GFM (tables, task lists, strike) +
 * `[[wikilinks]]`: a `[[Target]]` becomes a chip; clicking it calls
 * `onWikilink(title)` (the page scrolls to / opens that note). Plain links open
 * in a new tab. Styling comes from the scoped `.note-md` class (index.css) since
 * the Tailwind typography plugin isn't installed.
 */
export default function NoteMarkdown({
  body,
  onWikilink,
  className = '',
}: {
  body: string;
  onWikilink?: (title: string) => void;
  className?: string;
}) {
  return (
    <div className={`note-md ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkWikilink]}
        components={{
          a: ({ href, children }) => {
            if (href && href.startsWith(WIKILINK_HREF_PREFIX)) {
              const title = decodeURIComponent(href.slice(WIKILINK_HREF_PREFIX.length));
              return (
                <button
                  type="button"
                  className="note-wikilink"
                  onClick={(e) => {
                    e.preventDefault();
                    onWikilink?.(title);
                  }}
                >
                  {children}
                </button>
              );
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {body}
      </ReactMarkdown>
    </div>
  );
}
