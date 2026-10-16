import { useQuery, useQueryClient } from "@tanstack/react-query";

import { CONFIG_KEY } from "../../features/config";
import {
  SCHEDULE_KEY,
  ScheduleForm,
  definitionUnreadable,
  fetchSchedule,
} from "../../features/schedule";
import { CREDENTIALS_KEY, fetchCredentials } from "./CredentialsSection";

/**
 * Which hosted jobs run for the owner, and in which timezone (spec 120).
 * Hosted only: locally the schedule is launchd's, and the `schedule` kind
 * does not exist. It reads `GET /ops/schedule`, which carries the jobs on
 * offer, the owner's choice and the store's refusal of a stored row, and
 * the credentials, for the note beside a job that needs one.
 */
export function ScheduleChoiceSection() {
  const queryClient = useQueryClient();
  const schedule = useQuery({ queryKey: SCHEDULE_KEY, queryFn: fetchSchedule });
  const credentials = useQuery({ queryKey: CREDENTIALS_KEY, queryFn: fetchCredentials });

  if (schedule.isPending) return <p className="settings-muted">Reading the schedule…</p>;
  if (schedule.isError) {
    return (
      <p role="alert" className="settings-error">
        {schedule.error.message}
      </p>
    );
  }
  if (definitionUnreadable(schedule.data)) {
    // No form: with nothing on offer, a save could only store "no jobs" over
    // the owner's choice, and the fault is not theirs to fix.
    return (
      <p role="alert" className="settings-error">
        The schedule definition on the server could not be read: {schedule.data.error}. Nothing can
        be chosen until it is fixed; your stored choice is unchanged.
      </p>
    );
  }
  // Until the credentials are read, none counts as saved: the note can only
  // appear early, never be missed.
  const saved = new Set(
    (credentials.data?.credentials ?? []).filter((item) => item.set).map((item) => item.kind),
  );
  return (
    <ScheduleForm
      schedule={schedule.data}
      saved={saved}
      onSaved={() => {
        void queryClient.invalidateQueries({ queryKey: SCHEDULE_KEY });
        void queryClient.invalidateQueries({ queryKey: CONFIG_KEY });
      }}
    />
  );
}
