import QtQuick
import Quickshell.Io

// The panel's one backend process: `fetch-events.py --serve`. Each call is
// one JSON line on its stdin and gets one JSON line back with the same id.
// The process runs the calls in turn, so two writes never race, and no call
// pays Python's start-up. If it stops, pending calls fail and it restarts.
Item {
  id: backend
  visible: false

  property string script: ""
  property int nextId: 1
  // id -> { cmd, done }, for calls sent and not answered yet.
  property var pending: ({})
  // Lines asked for while the process was not running yet.
  property var waiting: []
  property string lastCmd: ""
  property int restartDelay: 1000
  readonly property bool syncRunning: Object.keys(pending).some(function(id) { return pending[id].cmd === "sync" })

  function hasPending(cmd) {
    return Object.keys(pending).some(function(id) { return pending[id].cmd === cmd })
  }

  // done(result) gets the backend's answer, or { status: "error", message }.
  function call(cmd, payload, done) {
    // A sync already waiting at the end of the line covers a new one.
    if (cmd === "sync" && lastCmd === "sync" && hasPending("sync")) return
    var id = nextId++
    var map = Object.assign({}, pending)
    map[id] = { cmd: cmd, done: done }
    pending = map
    lastCmd = cmd
    var line = JSON.stringify({ id: id, cmd: cmd, payload: payload === undefined ? null : payload })
    if (proc.running) proc.write(line + "\n")
    else {
      waiting = waiting.concat([line])
      if (!restartTimer.running) proc.running = true
    }
  }

  function answer(line) {
    var msg = null
    try { msg = JSON.parse(line) } catch (e) { return }
    if (!msg || !(msg.id in pending)) return
    var entry = pending[msg.id]
    var map = Object.assign({}, pending)
    delete map[msg.id]
    pending = map
    restartDelay = 1000
    if (entry.done) {
      try { entry.done(msg.result || {}) } catch (e) { console.warn("Chronica backend", e) }
    }
  }

  function failAll(message) {
    var list = pending
    pending = ({})
    waiting = []
    lastCmd = ""
    for (var id in list) {
      if (list[id].done) {
        try { list[id].done({ status: "error", message: message }) } catch (e) { console.warn("Chronica backend", e) }
      }
    }
  }

  Process {
    id: proc
    command: ["python3", backend.script, "--serve"]
    stdinEnabled: true
    running: backend.script !== ""
    onStarted: {
      var lines = backend.waiting
      backend.waiting = []
      for (var i = 0; i < lines.length; i++) write(lines[i] + "\n")
    }
    onRunningChanged: {
      if (running) return
      backend.failAll("The calendar backend stopped; it restarts by itself.")
      restartTimer.interval = backend.restartDelay
      // Back off up to a minute when it keeps failing (no python3, a crash).
      backend.restartDelay = Math.min(60000, backend.restartDelay * 2)
      restartTimer.start()
    }
    stdout: SplitParser {
      onRead: function(line) { backend.answer(line) }
    }
  }

  Timer {
    id: restartTimer
    onTriggered: proc.running = true
  }
}
