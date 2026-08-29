"""Constants for ``fractal``."""

#: fractal data directory at the repo root
FRACTAL_FOLDER = '.fractal'
#: directory holding the per-node git worktrees
WORKTREES_FOLDER = '.worktrees'
#: immutable project-layout cache under the worktrees directory
PROJECT_FOLDER = '.project'

#: per-node configuration file in the node's data directory
CONFIG_FILE = 'config.json'
#: central SQLite database file in the root node's data directory
DB_FILE = '.db'
#: lock filename, standalone or as a suffix, serializing concurrent access
LOCK_FILE = '.lock'
#: flock file serializing squash merges into one repo's targets
MERGE_LOCK_FILE = '.merge.lock'
#: self-ignore file in the user node's data directory (fractal track removes it)
SEED_IGNORE_FILE = '.gitignore'

# NOTE: the loop marker filenames below are git-ignored via the
#   _assets/git/exclude template -- retire or add markers in both
#   places in the same change (the exclude<->markers lockstep)
#: marker signaling the running loop to abort into a pause
PAUSE_ABORT_FILE = '.pause_abort'
#: tree-wide pause marker beside the central database
PAUSED_FILE = '.paused'
#: marker selecting the detached process backend for the node loop
HEADLESS_FILE = '.headless'
#: captured stdout and stderr for a headless node loop
HEADLESS_LOG = 'headless.log'
#: process group id of the running loop
PGID_FILE = '.pgid'
#: agent session records for the node
SESSION_FILE = '.session'
#: tmux server socket path of the session running the loop
SOCKET_FILE = '.socket'
#: the node's current lifecycle status
STATUS_FILE = '.status'
#: process group id of the running step
STEP_PGID_FILE = '.step_pgid'

#: lifecycle status set shared by the node status file and the record rows
STATUSES = (
    'active',
    'paused',
    'idle',
    'completed',
    'stopped',
    'exited',
    'killed',
    'failed',
    'retired',
)
#: point-in-time event kinds recorded on the node record
EVENTS = (
    'init',
    'spawn',
    'commit',
    'approve',
    'merge',
    'delete',
    'orphan',
    'model_drop',
    'start',
    'finish',
    'finish_cancel',
    'stop',
    'kill',
    'pause',
    'resume',
    'retire',
    'unretire',
)

#: lowest radio message priority
PRIORITY_MIN = 0
#: highest radio message priority
PRIORITY_MAX = 10
