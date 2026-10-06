import QtQuick
import Quickshell.Io

// Runs commands one at a time. A Process ignores `running = true` while it
// still runs, so two quick requests used to drop the second one (a lost
// notification, a lost config write). Jobs wait here instead.
Item {
  id: queue
  visible: false

  property var jobs: []
  property var current: null
  property string output: ""
  readonly property bool busy: current !== null

  // job: { command: [...], stdin: "text" (optional), key: "x" (optional),
  //        done: function(stdoutText) (optional) }
  // A job with a key replaces a waiting job with the same key, so a burst of
  // sync requests runs once, after whatever is running now.
  function enqueue(job) {
    var list = jobs.slice()
    if (job.key) list = list.filter(function(j) { return j.key !== job.key })
    list.push(job)
    jobs = list
    if (!busy) next()
  }

  function hasWaiting(key) {
    return jobs.some(function(j) { return j.key === key })
  }

  function next() {
    if (busy || jobs.length === 0) return
    var list = jobs.slice()
    current = list.shift()
    jobs = list
    proc.command = current.command
    proc.stdinEnabled = current.stdin !== undefined
    output = ""
    proc.running = true
  }

  function finish(text) {
    var job = current
    if (!job) return
    current = null
    if (job.done) {
      try { job.done(text) } catch (e) { console.warn("Chronica job", e) }
    }
    next()
  }

  Process {
    id: proc
    onStarted: {
      if (queue.current && queue.current.stdin !== undefined) write(queue.current.stdin + "\n")
    }
    // Move on only once the process stopped: a Process ignores `running =
    // true` until then. This also fires when the command could not start,
    // which sends no exited signal. callLater lets the collector deliver the
    // last output first.
    onRunningChanged: if (!running) Qt.callLater(function() { queue.finish(queue.output) })
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: queue.output = text
    }
  }
}
