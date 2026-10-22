import { useEffect, useRef, useState } from 'react';
import type { DiscoveryRequest, WebSearchResponse } from '../types';
import { webSearch } from '../api/client';

interface Props {
  /** The search that returned no match; sent only when the user asks. */
  request: DiscoveryRequest | null;
}

type State =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'done'; data: WebSearchResponse }
  | { kind: 'error'; code: string };

/** Team addition: optional web search, offered after a no-match result. Results are
 *  external links, kept apart from dataset recommendations and marked as unverified. */
export default function WebSearch({ request }: Props) {
  const [state, setState] = useState<State>({ kind: 'idle' });
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  if (!request) return null;

  async function run() {
    if (!request || state.kind === 'loading') return;
    controller.current?.abort();
    controller.current = new AbortController();
    setState({ kind: 'loading' });
    const result = await webSearch(request, controller.current.signal);
    setState(result.ok ? { kind: 'done', data: result.data } : { kind: 'error', code: result.error_code });
  }

  return (
    <div className="web-search">
      {state.kind === 'idle' && (
        <>
          <p className="muted">يمكننا البحث في مواقع خارجية موثوقة (الجهات الحكومية والجامعات والمنظمات غير الربحية، ودروب، ولينكدإن). النتائج غير موثقة من «فرصة».</p>
          <button className="btn btn--outline" onClick={run}>🔎 ابحث في مواقع خارجية موثوقة</button>
        </>
      )}

      {state.kind === 'loading' && (
        <div className="loading-row" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          <p className="muted">جارٍ البحث في المواقع الخارجية… قد يستغرق ذلك بضع ثوانٍ.</p>
        </div>
      )}

      {state.kind === 'error' && (
        <div role="alert">
          <p>تعذّر البحث في المواقع الخارجية الآن.</p>
          <div className="ref">المرجع: <span className="mono" dir="ltr">{state.code}</span></div>
          <button className="btn btn--quiet" onClick={run}>إعادة المحاولة</button>
        </div>
      )}

      {state.kind === 'done' && (
        <div aria-live="polite">
          <h3>نتائج من مواقع خارجية</h3>
          <p className="notice">{state.data.disclaimer}</p>
          <p>{state.data.message}</p>
          {state.data.results.map(r => (
            <div key={r.url} className="card">
              <h4>{r.title}</h4>
              <p className="muted" dir="ltr">{r.site}</p>
              {r.summary && <p>{r.summary}</p>}
              <a className="btn btn--quiet" href={r.url} target="_blank" rel="noopener noreferrer">فتح الموقع ↗</a>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
