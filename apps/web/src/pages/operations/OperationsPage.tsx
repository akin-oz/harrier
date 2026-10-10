import { RunPanel } from "../../features/runs/RunPanel";
import { useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory } from "../../features/runs/useRunStream";
import { DataChecksSection } from "./DataChecksSection";
import { DigestSection } from "./DigestSection";
import { DiscoverySection } from "./DiscoverySection";
import { EvaluationSection } from "./EvaluationSection";
import { FeedsSection } from "./FeedsSection";
import { HistorySection } from "./HistorySection";
import "./OperationsPage.css";
import { ReconsiderSection } from "./ReconsiderSection";
import { ScheduleSection } from "./ScheduleSection";
import { ScoringSection } from "./ScoringSection";

/**
 * What an operator does to keep the system running (spec 050, as amended by
 * spec 096), and the commands that work on the operator's data (spec 095).
 * Sections in the order an operator checks them: the schedule first,
 * because a job that stopped is the failure nothing else announces; then
 * discovery runs; then feed health, reconsideration and the digest; then
 * the data checks, decision history, batch evaluation and the learned
 * score's export.
 *
 * Configuration, backups and the list of commands with no button are on the
 * Settings page (spec 096), not here.
 */
export function OperationsPage({
  createEventSource = (url: string) => new EventSource(url),
}: {
  createEventSource?: EventSourceFactory;
} = {}) {
  // One stream for discovery: a run started with options streams on the
  // Runs panel, and the panel's own buttons share its lock state.
  const discovery = useRunStream(createEventSource);
  return (
    <div className="ops-page">
      <h2 className="ops-page__heading">Operations</h2>
      <ScheduleSection />
      <DiscoverySection stream={discovery} />
      {/* Discovery runs: the panel the Tracker page has, not a second one. */}
      <RunPanel createEventSource={createEventSource} stream={discovery} />
      <FeedsSection createEventSource={createEventSource} />
      <ReconsiderSection createEventSource={createEventSource} />
      <DigestSection createEventSource={createEventSource} />
      <DataChecksSection />
      <HistorySection createEventSource={createEventSource} />
      <EvaluationSection createEventSource={createEventSource} />
      <ScoringSection createEventSource={createEventSource} />
    </div>
  );
}
