const test = require("node:test");
const assert = require("node:assert/strict");
const Model = require("../Model.js");

test("Model.stepDate forward and backward", () => {
  assert.deepEqual(Model.stepDate("2026-09-16", 1), {
    dateKey: "2026-09-17",
    year: 2026,
    month: 8,
    day: 17
  });

  assert.deepEqual(Model.stepDate("2026-09-16", -1), {
    dateKey: "2026-09-15",
    year: 2026,
    month: 8,
    day: 15
  });

  assert.deepEqual(Model.stepDate("2026-09-16", 7), {
    dateKey: "2026-09-23",
    year: 2026,
    month: 8,
    day: 23
  });

  assert.deepEqual(Model.stepDate("2026-09-16", -7), {
    dateKey: "2026-09-09",
    year: 2026,
    month: 8,
    day: 9
  });
});

test("Model.stepDate across month and year boundaries", () => {
  // Cross into previous month
  assert.deepEqual(Model.stepDate("2026-09-01", -1), {
    dateKey: "2026-08-31",
    year: 2026,
    month: 7,
    day: 31
  });

  // Cross into next month
  assert.deepEqual(Model.stepDate("2026-09-30", 1), {
    dateKey: "2026-10-01",
    year: 2026,
    month: 9,
    day: 1
  });

  // Cross into previous year
  assert.deepEqual(Model.stepDate("2026-01-01", -1), {
    dateKey: "2025-12-31",
    year: 2025,
    month: 11,
    day: 31
  });

  // Cross into next year
  assert.deepEqual(Model.stepDate("2025-12-31", 1), {
    dateKey: "2026-01-01",
    year: 2026,
    month: 0,
    day: 1
  });

  // Leap year 2024 Feb 29
  assert.deepEqual(Model.stepDate("2024-03-01", -1), {
    dateKey: "2024-02-29",
    year: 2024,
    month: 1,
    day: 29
  });

  // Non-leap year 2025 Feb 28
  assert.deepEqual(Model.stepDate("2025-03-01", -1), {
    dateKey: "2025-02-28",
    year: 2025,
    month: 1,
    day: 28
  });
});

test("Model.stepToMonthBound", () => {
  assert.deepEqual(Model.stepToMonthBound("2026-09-16", "start"), {
    dateKey: "2026-09-01",
    year: 2026,
    month: 8,
    day: 1
  });

  assert.deepEqual(Model.stepToMonthBound("2026-09-16", "end"), {
    dateKey: "2026-09-30",
    year: 2026,
    month: 8,
    day: 30
  });

  assert.deepEqual(Model.stepToMonthBound("2024-02-10", "end"), {
    dateKey: "2024-02-29",
    year: 2024,
    month: 1,
    day: 29
  });
});

test("Model.stepToWeekBound", () => {
  // 2026-09-16 is a Wednesday (day 3)
  // Monday-start week: Monday is 2026-09-14, Sunday is 2026-09-20
  assert.deepEqual(Model.stepToWeekBound("2026-09-16", "start", 1), {
    dateKey: "2026-09-14",
    year: 2026,
    month: 8,
    day: 14
  });

  assert.deepEqual(Model.stepToWeekBound("2026-09-16", "end", 1), {
    dateKey: "2026-09-20",
    year: 2026,
    month: 8,
    day: 20
  });

  // Sunday-start week: Sunday is 2026-09-13, Saturday is 2026-09-19
  assert.deepEqual(Model.stepToWeekBound("2026-09-16", "start", 0), {
    dateKey: "2026-09-13",
    year: 2026,
    month: 8,
    day: 13
  });

  assert.deepEqual(Model.stepToWeekBound("2026-09-16", "end", 0), {
    dateKey: "2026-09-19",
    year: 2026,
    month: 8,
    day: 19
  });
});
