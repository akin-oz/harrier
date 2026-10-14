import { SignOutButton } from "../../features/auth/SignOutButton";
import { HealthBadge } from "../../features/health/HealthBadge";
import { TrackSwitcher } from "../../features/tracks/TrackSwitcher";
import { appMode } from "../../shared/config/appMode";
import "./Header.css";

// The track switcher sits beside the health badge (spec 094): which search
// the page works in is the first thing to know about everything below it.
export function Header({ onManageTracks }: { onManageTracks: () => void }) {
  // Sign-out is a hosted-mode control: locally there is no session (spec 114).
  const hosted = appMode().kind === "hosted";
  return (
    <header className="app-header">
      <h1 className="app-header__title">Harrier</h1>
      <div className="app-header__end">
        <TrackSwitcher onManage={onManageTracks} />
        <HealthBadge />
        {hosted && <SignOutButton />}
      </div>
    </header>
  );
}
