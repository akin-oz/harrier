import { HealthBadge } from "../../features/health/HealthBadge";
import "./Header.css";

export function Header() {
  return (
    <header className="app-header">
      <h1 className="app-header__title">Harrier</h1>
      <HealthBadge />
    </header>
  );
}
