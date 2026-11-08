import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FOLLOW_BOTTOM,
  FOLLOW_OFF,
  FOLLOW_TURN_START,
  REPLY_ANCHOR_MARGIN_PX,
  anchoredScrollTop,
  maxScrollTopOf,
  planMessagesScroll,
  topWithinScroller,
} from '../chatScroll.js';

const user = (content = 'question') => ({ role: 'user', content });
const assistant = (content = 'answer') => ({ role: 'assistant', content });
const system = (content = '[MODE] agent') => ({ role: 'system', content });

test('a message the user sends scrolls down and waits for the reply', () => {
  for (const mode of [FOLLOW_BOTTOM, FOLLOW_OFF, FOLLOW_TURN_START]) {
    assert.deepEqual(
      planMessagesScroll(mode, [assistant('hi'), user()]),
      { mode: FOLLOW_TURN_START, action: 'bottom', index: 1 },
    );
  }
});

test('system messages after the user message do not hide it', () => {
  assert.deepEqual(
    planMessagesScroll(FOLLOW_OFF, [user(), system()]),
    { mode: FOLLOW_TURN_START, action: 'bottom', index: 0 },
  );
});

test('the reply to the message just sent is anchored at its start', () => {
  assert.deepEqual(
    planMessagesScroll(FOLLOW_TURN_START, [user(), assistant()]),
    { mode: FOLLOW_TURN_START, action: 'anchor', index: 1 },
  );
  assert.deepEqual(
    planMessagesScroll(FOLLOW_TURN_START, [user(), assistant(), system()]),
    { mode: FOLLOW_TURN_START, action: 'anchor', index: 1 },
  );
});

test('history loaded into an opened chat keeps following the bottom', () => {
  assert.deepEqual(
    planMessagesScroll(FOLLOW_BOTTOM, [user(), assistant()]),
    { mode: FOLLOW_BOTTOM, action: 'bottom', index: -1 },
  );
});

test('a user who scrolled away is left where they are', () => {
  assert.deepEqual(
    planMessagesScroll(FOLLOW_OFF, [user(), assistant()]),
    { mode: FOLLOW_OFF, action: 'none', index: -1 },
  );
  assert.deepEqual(
    planMessagesScroll(FOLLOW_OFF, []),
    { mode: FOLLOW_OFF, action: 'none', index: -1 },
  );
});

test('a long reply puts its start at the top of the viewport', () => {
  assert.equal(anchoredScrollTop(1000, 5000), 1000 - REPLY_ANCHOR_MARGIN_PX);
  assert.equal(anchoredScrollTop(1000, 5000, 0), 1000);
});

test('a short reply ends at the bottom, where following would leave it', () => {
  assert.equal(anchoredScrollTop(1000, 600), 600);
});

test('the anchored position is never negative', () => {
  assert.equal(anchoredScrollTop(3, 600), 0);
  assert.equal(anchoredScrollTop(100, -20), 0);
});

test('a missing anchor falls back to the bottom', () => {
  assert.equal(anchoredScrollTop(Number.NaN, 600), 600);
  assert.equal(anchoredScrollTop(undefined, 600), 600);
});

test('the maximum scroll position is the content past the viewport', () => {
  assert.equal(maxScrollTopOf({ scrollHeight: 2000, clientHeight: 500 }), 1500);
  assert.equal(maxScrollTopOf({ scrollHeight: 300, clientHeight: 500 }), 0);
});

function fakeScroller({ top, height, scrollTop, zoom = 1, clientTop = 0 }) {
  return {
    scrollTop,
    clientTop,
    offsetHeight: height,
    getBoundingClientRect: () => ({ top, height: height * zoom }),
  };
}

const fakeElement = (top) => ({ getBoundingClientRect: () => ({ top }) });

test('an element is located in the scroller content coordinates', () => {
  const scroller = fakeScroller({ top: 100, height: 400, scrollTop: 250 });
  // 50px below the scroller's top edge, with 250px already scrolled past.
  assert.equal(topWithinScroller(scroller, fakeElement(150)), 300);
});

test('the chat zoom is undone before adding the scroll offset', () => {
  // At 150% zoom, 75 viewport pixels are 50 layout pixels.
  const scroller = fakeScroller({ top: 100, height: 400, scrollTop: 250, zoom: 1.5 });
  assert.equal(topWithinScroller(scroller, fakeElement(175)), 300);
});

test('the scroller border is not counted as content', () => {
  const scroller = fakeScroller({ top: 100, height: 400, scrollTop: 0, clientTop: 1 });
  assert.equal(topWithinScroller(scroller, fakeElement(151)), 50);
});
