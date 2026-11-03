// The backend runs one agent turn at a time and refuses /api/gilzabots/run with
// 409 while another turn is still alive — typically one whose stream this window
// lost. These are the reasons it reports.
export const RUN_CONFLICT_TURN_ACTIVE = 'turn_active';
export const RUN_CONFLICT_TURN_STOPPING = 'turn_stopping';

/**
 * Return `{ reason, error }` when a /api/gilzabots/run response refused to start
 * a turn because another one is alive, or null for any other response.
 */
export async function readRunConflict(res) {
  if (!res || res.status !== 409) return null;
  let payload = {};
  try {
    payload = await res.json();
  } catch (e) {
    payload = {};
  }
  return {
    reason: typeof payload?.reason === 'string' ? payload.reason : '',
    error: typeof payload?.error === 'string' ? payload.error : '',
  };
}
