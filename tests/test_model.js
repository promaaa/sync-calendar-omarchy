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

test("Model.parseTimeInput accepts 24h and 12h input, rejects garbage", () => {
  const cases = {
    "14:30": "14:30", "9:05": "09:05", "9": "09:00", "1430": "14:30", "14.30": "14:30",
    "2pm": "14:00", "2:30 PM": "14:30", "11:15 a.m.": "11:15", "12am": "00:00", "12pm": "12:00",
  };
  for (const [input, expected] of Object.entries(cases)) {
    assert.equal(Model.parseTimeInput(input), expected, input);
  }
  for (const input of ["", "24:00", "13pm", "0am", "9:60", "noon", "9:5"]) {
    assert.equal(Model.parseTimeInput(input), "", input);
  }
  assert.equal(Model.calculateEndTime("2:30pm", 30), "15:00");
});

test("Model.parseDateInput accepts real days only", () => {
  assert.equal(Model.parseDateInput("2026-09-14"), "2026-09-14");
  assert.equal(Model.parseDateInput(" 2026-9-4 "), "2026-09-04");
  assert.equal(Model.parseDateInput("2028-02-29"), "2028-02-29");
  for (const input of ["", "2026-02-29", "2026-13-01", "2026-04-31", "14/09/2026", "tomorrow"]) {
    assert.equal(Model.parseDateInput(input), "", input);
  }
});

test("Model.notificationStage staged and single reminders", () => {
  assert.equal(Model.notificationStage(10, "staged"), 10);
  assert.equal(Model.notificationStage(4, "staged"), 5);
  assert.equal(Model.notificationStage(0, "staged"), 1);
  assert.equal(Model.notificationStage(11, "staged"), null);
  assert.equal(Model.notificationStage(14, "15"), 15);
  assert.equal(Model.notificationStage(16, "15"), null);
});

test("Model.dueNotifications sends each due reminder once", () => {
  const now = Date.parse("2026-10-06T09:55:00");
  const at = (h, m) => new Date(2026, 9, 6, h, m).toISOString();
  const events = [
    { id: "a", title: "Standup", startIso: at(10, 0), startTime: "10:00", calendar: "Work", location: "Room 1" },
    { id: "b", title: "Review", startIso: at(10, 0), startTime: "10:00", meetingProvider: "Zoom" },
    { id: "c", title: "Later", startIso: at(11, 0), startTime: "11:00" },
    { id: "d", title: "Holiday", allDay: true, startIso: at(0, 0) },
    // A multi-day event appears on two days: one reminder.
    { id: "b", title: "Review", startIso: at(10, 0), startTime: "10:00", meetingProvider: "Zoom" },
  ];
  const due = Model.dueNotifications(events, now, "staged", {}, () => "10:00 - 10:30");
  assert.deepEqual(due.map((d) => d.title), ["Upcoming in 5m: Standup", "Upcoming in 5m: Review"]);
  assert.equal(due[0].body, "[Work]  ·  10:00 - 10:30  ·  📍 Room 1");
  assert.equal(due[1].body, "10:00 - 10:30  ·  📹 Zoom");

  const sent = { [due[0].key]: true };
  assert.deepEqual(Model.dueNotifications(events, now, "staged", sent, () => "").map((d) => d.title),
                   ["Upcoming in 5m: Review"]);
});

test("Model.upcomingEvents flattens days with one heading each", () => {
  const byDate = {
    "2026-10-06": [{ id: "a", calendar: "Work" }, { id: "b", calendar: "Home" }],
    "2026-10-08": [{ id: "c", calendar: "Home" }],
    "2026-10-20": [{ id: "late" }],
  };
  const all = Model.upcomingEvents(byDate, "2026-10-06", 7);
  assert.deepEqual(all.map((e) => [e.id, e.dayHeading || ""]), [["a", "2026-10-06"], ["b", ""], ["c", "2026-10-08"]]);
  const home = Model.upcomingEvents(byDate, "2026-10-06", 7, (e) => e.calendar === "Home");
  assert.deepEqual(home.map((e) => [e.id, e.dayHeading]), [["b", "2026-10-06"], ["c", "2026-10-08"]]);
});

test("Model.parseQuickAdd understands dates and times in English and French", () => {
  const today = "2026-10-06"; // a Tuesday
  const q = (text) => Model.parseQuickAdd(text, today);
  assert.deepEqual(q("Lunch tomorrow 1pm"), { title: "Lunch", date: "2026-10-07", start: "13:00", end: "14:00" });
  assert.deepEqual(q("Standup friday 9:30-9:45"), { title: "Standup", date: "2026-10-09", start: "09:30", end: "09:45" });
  assert.deepEqual(q("Dentiste demain à 14h30"), { title: "Dentiste", date: "2026-10-07", start: "14:30", end: "15:30" });
  assert.deepEqual(q("Call 1-2pm"), { title: "Call", date: "", start: "13:00", end: "14:00" });
  assert.deepEqual(q("Gym tuesday at 7 for 90 min"), { title: "Gym", date: "2026-10-06", start: "07:00", end: "08:30" });
  assert.deepEqual(q("Retro next tuesday 16h-17h"), { title: "Retro", date: "2026-10-13", start: "16:00", end: "17:00" });
  assert.deepEqual(q("Review 2026-10-12"), { title: "Review", date: "2026-10-12", start: "", end: "" });
  assert.deepEqual(q("Trip in 3 days"), { title: "Trip", date: "2026-10-09", start: "", end: "" });
  // Numbers and short words that are not dates or times stay in the title.
  assert.deepEqual(q("Lunch with Sam 2-3 people"), { title: "Lunch with Sam 2-3 people", date: "", start: "", end: "" });
  assert.deepEqual(q("Read chapter 9"), { title: "Read chapter 9", date: "", start: "", end: "" });
});
