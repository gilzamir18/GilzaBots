// Resolving an edit at the moment it is handed off, not when its turn ends.
//
// Sending a message runs the whole agent turn before its promise settles, which
// can take minutes. The message editor needs an earlier answer: may it let go
// of the text, or must it keep it and say why? `start` receives two callbacks
// and returns the send's promise:
//
//   onHandoff()       the message is on its way; the editor can close
//   onRefused(error)  nothing was sent, and this is why
//
// The result is { accepted: true } or { accepted: false, error }, and it is
// always one of them: a send that ends (or fails) without calling either
// callback was refused somewhere that gave no reason, and is reported with
// `notSentMessage` rather than left pending or taken as success.

export function settleAtHandoff(start, notSentMessage) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (result) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    let sending;
    try {
      sending = start({
        onHandoff: () => settle({ accepted: true }),
        onRefused: (error) => settle({ accepted: false, error: error || notSentMessage }),
      });
    } catch (err) {
      settle({ accepted: false, error: err?.message || String(err) });
      return;
    }
    Promise.resolve(sending).then(
      () => settle({ accepted: false, error: notSentMessage }),
      (err) => settle({ accepted: false, error: err?.message || String(err) }),
    );
  });
}
