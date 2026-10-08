import { HealthBadge } from "../../features/health/HealthBadge";
import { TrackSwitcher } from "../../features/tracks/TrackSwitcher";
import "./Header.css";

// The track switcher sits beside the health badge (spec 094): which search
// the page works in is the first thing to know about everything below it.
export function Header({ onManageTracks }: { onManageTracks: () => void }) {
  return (
    <header className="app-header">
      <h1 className="app-header__title">Harrier</h1>
      <div className="app-header__end">
        <TrackSwitcher onManage={onManageTracks} />
        <HealthBadge />
      </div>
    </header>
  );
}
