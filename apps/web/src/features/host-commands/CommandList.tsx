import { CopyCommand } from "./CopyCommand";
import { useCommandPlaces } from "./useCommandPlaces";
import "./hostCommands.css";

/**
 * Every CLI command in its one place: run on the host, terminal only, or
 * routed (spec 096). Rendered from the API, never from a list typed here, so
 * the page cannot claim more or less than the server and its test hold.
 */
export function CommandList() {
  const places = useCommandPlaces();

  if (places.isPending) return <p className="host-muted">Reading the command list…</p>;
  if (places.isError) {
    return (
      <p role="alert" className="host-error">
        {places.error.message}
      </p>
    );
  }
  const { host, terminal, routed } = places.data;

  return (
    <div className="command-list">
      <section aria-labelledby="commands-host">
        <h4 id="commands-host" className="command-list__heading">
          Run on the host ({host.length})
        </h4>
        <ul className="command-list__items">
          {host.map((entry) => (
            <li key={entry.command} className="command-list__item">
              <CopyCommand command={entry.shown} />
              <p className="command-list__reason">{entry.reason}</p>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="commands-terminal">
        <h4 id="commands-terminal" className="command-list__heading">
          Terminal only ({terminal.length})
        </h4>
        <ul className="command-list__items">
          {terminal.map((entry) => (
            <li key={entry.command} className="command-list__item">
              <code className="command-list__command">harrier {entry.command}</code>
              <p className="command-list__reason">{entry.reason}</p>
            </li>
          ))}
        </ul>
      </section>

      <details className="command-list__routed">
        <summary>Run from the browser ({routed.length})</summary>
        <table className="command-list__table">
          <thead>
            <tr>
              <th scope="col">Command</th>
              <th scope="col">Route</th>
            </tr>
          </thead>
          <tbody>
            {routed.map((entry) => (
              <tr key={entry.command}>
                <td>
                  <code>harrier {entry.command}</code>
                </td>
                <td>
                  <code>{entry.route}</code>
                  {entry.note !== null && <span className="command-list__note">{entry.note}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}
