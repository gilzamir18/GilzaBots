// Matches model-provided "other"/"none of the above" style choices (en/pt/es).
// The UI owns one write-in option, so equivalent model choices are removed.
const OTHER_OPTION_PATTERN = /^(outr[oa]s?|other|otro)\b/i;

export const askQuestionOptions = (options) => {
  if (!Array.isArray(options) || options.length === 0) return null;
  const filtered = options.filter((option) => !OTHER_OPTION_PATTERN.test(String(option).trim()));
  return filtered.length > 0 ? filtered : null;
};

/**
 * Apply defaults according to the request contract. Ask requests must never
 * inherit the yes/no choices or the affirmative default used by confirmations.
 */
export const normalizeInputRequest = (data = {}) => {
  const type = data.type || 'confirm';
  const request = { ...data, id: data.id, prompt: data.prompt, type };

  if (data.options === undefined || data.options === null) {
    if (type !== 'ask') request.options = ['yes', 'no'];
    else delete request.options;
  }
  if (data.default === undefined || data.default === null) {
    if (type !== 'ask') request.default = 'yes';
    else delete request.default;
  }
  return request;
};

/**
 * Choose the dialog that renders a `confirmRequest`. Local prompts (new file,
 * new directory, new presentation) travel through `confirmRequest` with
 * `type: 'ask'` and a callback; they need a text input, so they must never fall
 * through to the Yes/No confirmation, whose button value would reach the
 * callback as the answer.
 */
export const confirmRequestDialog = (request) => {
  if (!request) return null;
  if (request.type === 'interactive_terminal') return 'interactive_terminal';
  if (request.type === 'ask') return 'ask';
  return 'confirm';
};

const localRequestKeys = new WeakMap();
let nextLocalRequestKey = 1;

/**
 * Stable React key for a dialog request. Backend requests carry an id; local
 * prompts do not, so each request object gets its own counter value. The input
 * window is non-modal, so a second prompt can replace the first while it is
 * open; a fresh key remounts the window and keeps the first prompt's typed text
 * from being submitted to the second prompt's callback.
 */
export const dialogRequestKey = (request) => {
  if (!request) return null;
  if (request.id !== undefined && request.id !== null) return `id:${request.id}`;
  if (!localRequestKeys.has(request)) localRequestKeys.set(request, `local:${nextLocalRequestKey++}`);
  return localRequestKeys.get(request);
};

/**
 * State updater that closes an agent input window once the backend accepted its
 * answer. Accepting the answer releases the agent immediately, and the agent's
 * next request can arrive before the answer's POST returns; it replaces the
 * slot, and must not be cleared along with the request that was answered.
 */
export const clearAnsweredRequest = (answeredId) => (current) => (
  current && current.id === answeredId ? null : current
);

/** Return the wire value expected by /api/gilzabots/input_response. */
export const formatAskResponse = ({
  inputValue = '',
  isMultiSelect = false,
  isOtherSelected = false,
  options,
  selectedIndexes = [],
}) => {
  if (!options || options.length === 0) return inputValue;

  if (!isMultiSelect) {
    if (isOtherSelected) return inputValue;
    const [selectedIndex] = selectedIndexes;
    return Number.isInteger(selectedIndex) && options[selectedIndex] !== undefined
      ? String(options[selectedIndex])
      : '';
  }

  const selected = [...selectedIndexes]
    .filter((index) => Number.isInteger(index) && options[index] !== undefined)
    .sort((left, right) => left - right)
    .map((index) => String(options[index]));
  const custom = inputValue.trim();
  if (isOtherSelected && custom) selected.push(custom);
  return JSON.stringify(selected);
};
