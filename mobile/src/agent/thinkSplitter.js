/**
 * Route `<think>…</think>` written inside the content channel to reasoning.
 *
 * Models served through an OpenAI-compatible endpoint (Ollama, DeepSeek R1
 * distills) often stream their reasoning as tagged text instead of a separate
 * field. A tag can be split across chunks, so a trailing partial tag is held
 * back until the next chunk decides it. Mirrors the desktop's
 * `gilzabots/think_stream.py` for balanced tags.
 */

const OPEN = '<think>';
const CLOSE = '</think>';

export function createThinkSplitter({ onVisible, onThinking }) {
  let buffer = '';
  let inThink = false;

  const partialTagLength = (text, tag) => {
    for (let length = Math.min(tag.length - 1, text.length); length > 0; length -= 1) {
      if (tag.startsWith(text.slice(-length).toLowerCase())) return length;
    }
    return 0;
  };

  function feed(chunk) {
    buffer += String(chunk ?? '');
    for (;;) {
      const tag = inThink ? CLOSE : OPEN;
      const index = buffer.toLowerCase().indexOf(tag);
      if (index >= 0) {
        const before = buffer.slice(0, index);
        if (before) (inThink ? onThinking : onVisible)(before);
        buffer = buffer.slice(index + tag.length);
        inThink = !inThink;
        continue;
      }
      const hold = partialTagLength(buffer, tag);
      const ready = buffer.slice(0, buffer.length - hold);
      if (ready) (inThink ? onThinking : onVisible)(ready);
      buffer = buffer.slice(buffer.length - hold);
      return;
    }
  }

  function flush() {
    if (buffer) (inThink ? onThinking : onVisible)(buffer);
    buffer = '';
    inThink = false;
  }

  return { feed, flush };
}
