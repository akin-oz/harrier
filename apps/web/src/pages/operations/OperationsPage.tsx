import { RunPanel } from "../../features/runs/RunPanel";
import type { EventSourceFactory } from "../../features/runs/useRunStream";
import { DigestSection } from "./DigestSection";
import { FeedsSection } from "./FeedsSection";
import "./OperationsPage.css";
import { ReconsiderSection } from "./ReconsiderSection";
import { ScheduleSection } from "./ScheduleSection";

/**
 * What an operator does to keep the system running (spec 050, as amended by
 * spec 096). Sections in the order an operator checks them: the schedule
 * first, because a job that stopped is the failure nothing else announces;
 * then discovery runs; then feed health, reconsideration and the digest.
 *
 * Configuration, backups and the list of commands with no button are on the
 * Settings page (spec 096), not here.
 */
export function OperationsPage({
  createEventSource = (url: string) => new EventSource(url),
}: {
  createEventSource?: EventSourceFactory;
} = {}) {
  return (
    <div className="ops-page">
      <h2 className="ops-page__heading">Operations</h2>
      <ScheduleSection />
      {/* Discovery runs: the panel the Tracker page has, not a second one. */}
      <RunPanel createEventSource={createEventSource} />
      <FeedsSection createEventSource={createEventSource} />
      <ReconsiderSection createEventSource={createEventSource} />
      <DigestSection createEventSource={createEventSource} />
    </div>
  );
}
