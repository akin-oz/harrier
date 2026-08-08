import type { Job } from "./types";

export function JobTable({ jobs }: { jobs: readonly Job[] }) {
  if (jobs.length === 0) {
    return <p>No jobs match.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Company</th>
          <th>Title</th>
          <th>Location</th>
          <th>Status</th>
          <th>Score</th>
          <th>Source</th>
          <th>Added</th>
          <th>Next action</th>
        </tr>
      </thead>
      <tbody>
        {jobs.map((job) => (
          <tr key={job.id}>
            <td>{job.company}</td>
            <td>
              {job.url ? (
                <a href={job.url} target="_blank" rel="noreferrer">
                  {job.title}
                </a>
              ) : (
                job.title
              )}
            </td>
            <td>{job.location}</td>
            <td>{job.status}</td>
            <td>{job.score || job.fit_score}</td>
            <td>{job.source}</td>
            <td>{job.added_at}</td>
            <td>{job.next_action}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
