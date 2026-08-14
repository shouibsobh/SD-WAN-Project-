import time
import sqlite3
from pathlib import Path


_path_announced = False


class DatabaseManager:

    def __init__(self):

        global _path_announced

        project_root = Path(__file__).resolve().parent.parent

        db_dir = project_root / "database"
        db_dir.mkdir(exist_ok=True)

        db_path = db_dir / "telemetry.db"

        if not _path_announced:
            print("Using database:", db_path)
            _path_announced = True

        self.connection = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=30
        )
        # Allow accessing columns by name
        self.connection.row_factory = sqlite3.Row

        # Enable concurrent read/write access
        self.connection.execute("PRAGMA journal_mode=WAL;")
        self.connection.execute("PRAGMA synchronous=NORMAL;")
        self.connection.execute("PRAGMA busy_timeout=30000;")

        self.create_tables()

    def create_tables(self):

        cursor = self.connection.cursor()

        # ------------------------------------------------------------
        # Discrete network/SLA events raised by telemetry_agent.py and
        # telemetry/sla_collector.py (state transitions only, not raw
        # samples -- those live in Prometheus).
        # ------------------------------------------------------------
        cursor.execute("""

        CREATE TABLE IF NOT EXISTS network_events(

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp REAL,

            event_type TEXT,

            switch_id INTEGER,

            port_no INTEGER,

            severity TEXT,

            description TEXT

        )

        """)

        self.connection.commit()

        # Current health state per (switch_id, port_no) directed link
        cursor.execute("""

        CREATE TABLE IF NOT EXISTS link_status (

            switch_id INTEGER,

            port_no INTEGER,

            state TEXT,

            last_change REAL,

            PRIMARY KEY (switch_id, port_no)
        )

        """)

        self.connection.commit()

        # Current SLA health state per (src_host, dst_host) pair.
        cursor.execute("""

        CREATE TABLE IF NOT EXISTS sla_status (

            src_host TEXT,

            dst_host TEXT,

            state TEXT,

            last_change REAL,

            PRIMARY KEY (src_host, dst_host)
        )

        """)

        self.connection.commit()

        # ------------------------------------------------------------
        # Agent decisions (decide [RL] -> guardrail -> execute).
        # ------------------------------------------------------------
        cursor.execute("""

        CREATE TABLE IF NOT EXISTS agent_decisions (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp REAL,

            metric_type TEXT,

            anomaly_state TEXT,

            action TEXT,

            target TEXT,

            reasoning TEXT,

            approved INTEGER,

            guardrail_reason TEXT,

            executed INTEGER,

            execution_result TEXT

        )

        """)

        self.connection.commit()

        # ------------------------------------------------------------
        # history of every chaos-engineering action
        # ------------------------------------------------------------
        cursor.execute("""

        CREATE TABLE IF NOT EXISTS chaos_log (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp REAL,

            switch_a INTEGER,

            switch_b INTEGER,

            fault_type TEXT,

            parameters TEXT,

            result TEXT

        )

        """)

        self.connection.commit()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS rl_transitions (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp REAL,

            metric_type TEXT,
            anomaly_state TEXT,

            action TEXT,
            target TEXT,          -- JSON
            features TEXT,        -- JSON list, the feature vector that was scored

            reward REAL,           -- NULL until resolved
            resolved INTEGER DEFAULT 0,

            sla_before TEXT,       -- JSON list [latency, jitter, loss], each
                                   -- normalized to [0,1], or null per field

            approved INTEGER DEFAULT 0,
            executed INTEGER DEFAULT 0

        )
        """)
        self.connection.commit()

        for column, coltype in (
            ("approved", "INTEGER DEFAULT 0"),
            ("executed", "INTEGER DEFAULT 0"),
            ("sla_before", "TEXT"),
        ):
            try:
                cursor.execute(
                    f"ALTER TABLE rl_transitions ADD COLUMN {column} {coltype}"
                )
                self.connection.commit()
            except sqlite3.OperationalError:
                pass  # column already exists -- nothing to do

        # ------------------------------------------------------------
        # VXLAN tunnel registry: written by tunnel_manager.py, read by 
        #sla_collector.py to know which switch pairs to probe.
        # ------------------------------------------------------------
        cursor.execute("""

        CREATE TABLE IF NOT EXISTS tunnels (

            switch_a INTEGER,

            switch_b INTEGER,

            vni INTEGER,

            port_a INTEGER,

            port_b INTEGER,

            remote_ip_a TEXT,

            remote_ip_b TEXT,

            created_at REAL,

            PRIMARY KEY (switch_a, switch_b)

        )

        """)

        self.connection.commit()

    # ------------------------------------------------------------------
    # Network / link events
    # ------------------------------------------------------------------

    def insert_event(
            self,
            event_type,
            switch_id,
            port_no,
            severity,
            description
    ):

        cursor = self.connection.cursor()

        cursor.execute("""

        INSERT INTO network_events(
            timestamp,
            event_type,
            switch_id,
            port_no,
            severity,
            description
        )

        VALUES(?,?,?,?,?,?)

        """, (
            time.time(),
            event_type,
            switch_id,
            port_no,
            severity,
            description
        ))

        self.connection.commit()

    def get_recent_events(self, limit=100):

        cursor = self.connection.cursor()

        cursor.execute("""

            SELECT *

            FROM network_events

            ORDER BY id DESC

            LIMIT ?

        """, (limit,))

        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def count_events(self, since_seconds=None):
        """Count network_events rows, optionally within the last
        `since_seconds`.

        A COUNT(*) rather than len(get_recent_events()), which is capped
        by `limit` and would undercount past that cap.
        """

        cursor = self.connection.cursor()

        if since_seconds is not None:
            cursor.execute("""
                SELECT COUNT(*) as c
                FROM network_events
                WHERE timestamp >= ?
            """, (time.time() - since_seconds,))
        else:
            cursor.execute("SELECT COUNT(*) as c FROM network_events")

        return cursor.fetchone()["c"]

    # ------------------------------------------------------------------
    # Link state tracking
    # ------------------------------------------------------------------

    def get_link_state(self, switch_id, port_no):

        cursor = self.connection.cursor()

        cursor.execute("""

            SELECT state

            FROM link_status

            WHERE switch_id=?

            AND port_no=?

        """, (switch_id, port_no))

        row = cursor.fetchone()

        if row is None:
            return None

        return row["state"]

    def update_link_state(self, switch_id, port_no, state):

        cursor = self.connection.cursor()

        cursor.execute("""

            INSERT INTO link_status

            VALUES(?,?,?,?)

            ON CONFLICT(switch_id,port_no)

            DO UPDATE SET

                state=excluded.state,

                last_change=excluded.last_change

        """,

        (

            switch_id,

            port_no,

            state,

            time.time()

        ))

        self.connection.commit()

    # ------------------------------------------------------------------
    # SLA state tracking Used  for per-tunnel SLA (src_host/dst_host = "tunnel:sA"/"tunnel:sB"
    # ------------------------------------------------------------------

    def get_sla_state(self, src_host, dst_host):

        cursor = self.connection.cursor()

        cursor.execute("""

            SELECT state

            FROM sla_status

            WHERE src_host=?

            AND dst_host=?

        """, (src_host, dst_host))

        row = cursor.fetchone()

        if row is None:
            return None

        return row["state"]

    def update_sla_state(self, src_host, dst_host, state):

        cursor = self.connection.cursor()

        cursor.execute("""

            INSERT INTO sla_status

            VALUES(?,?,?,?)

            ON CONFLICT(src_host,dst_host)

            DO UPDATE SET

                state=excluded.state,

                last_change=excluded.last_change

        """,

        (

            src_host,

            dst_host,

            state,

            time.time()

        ))

        self.connection.commit()

    # ------------------------------------------------------------------
    # Agent decisions
    # ------------------------------------------------------------------

    def insert_agent_decision(self, decision):

        cursor = self.connection.cursor()

        cursor.execute("""

            INSERT INTO agent_decisions(

                timestamp,
                metric_type,
                anomaly_state,
                action,
                target,
                reasoning,
                approved,
                guardrail_reason,
                executed,
                execution_result

            )

            VALUES(?,?,?,?,?,?,?,?,?,?)

        """, (

            decision["timestamp"],
            decision["metric_type"],
            decision["anomaly_state"],
            decision["action"],
            decision["target"],
            decision["reasoning"],
            int(bool(decision["approved"])),
            decision["guardrail_reason"],
            int(bool(decision["executed"])),
            decision["execution_result"],
        ))

        self.connection.commit()

    def get_recent_agent_decisions(self, limit=100):

        cursor = self.connection.cursor()

        cursor.execute("""

            SELECT *

            FROM agent_decisions

            ORDER BY id DESC

            LIMIT ?

        """, (limit,))

        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def get_recent_reroute_decisions(self, metric_type, limit=200):

        cursor = self.connection.cursor()

        cursor.execute("""

            SELECT * FROM agent_decisions
            WHERE metric_type = ?
            AND action = 'REROUTE'
            AND approved = 1
            AND executed = 1
            ORDER BY id DESC
            LIMIT ?

        """, (metric_type, limit))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Chaos engineering log
    # ------------------------------------------------------------------

    def insert_chaos_event(self, switch_a, switch_b, fault_type,
                            parameters, result):

        cursor = self.connection.cursor()

        cursor.execute("""

            INSERT INTO chaos_log(

                timestamp,
                switch_a,
                switch_b,
                fault_type,
                parameters,
                result

            )

            VALUES(?,?,?,?,?,?)

        """, (
            time.time(),
            switch_a,
            switch_b,
            fault_type,
            parameters,
            result,
        ))

        self.connection.commit()

    def get_recent_chaos_events(self, limit=100):

        cursor = self.connection.cursor()

        cursor.execute("""

            SELECT *

            FROM chaos_log

            ORDER BY id DESC

            LIMIT ?

        """, (limit,))

        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # RL transitions (decision_node.py writes, telemetry_agent.py
    # backfills approved/executed, reward_tracker.py resolves)
    # ------------------------------------------------------------------

    def insert_rl_transition(self, transition):

        cursor = self.connection.cursor()

        cursor.execute("""

            INSERT INTO rl_transitions(
                timestamp, metric_type, anomaly_state,
                action, target, features, sla_before,
                reward, resolved, approved, executed
            )
            VALUES(?,?,?,?,?,?,?,NULL,0,0,0)

        """, (
            transition["timestamp"],
            transition["metric_type"],
            transition["anomaly_state"],
            transition["action"],
            transition["target"],
            transition["features"],
            transition.get("sla_before"),
        ))

        self.connection.commit()
        return cursor.lastrowid

    def update_rl_transition_outcome(self, transition_id, approved, executed):

        cursor = self.connection.cursor()

        cursor.execute("""

            UPDATE rl_transitions
            SET approved = ?, executed = ?
            WHERE id = ?

        """, (int(bool(approved)), int(bool(executed)), transition_id))

        self.connection.commit()

    def get_pending_rl_transitions(self, older_than_seconds):

        cursor = self.connection.cursor()

        cursor.execute("""

            SELECT * FROM rl_transitions
            WHERE resolved = 0
            AND timestamp <= ?

        """, (time.time() - older_than_seconds,))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def resolve_rl_transition(self, transition_id, reward):

        cursor = self.connection.cursor()

        cursor.execute("""

            UPDATE rl_transitions
            SET reward = ?, resolved = 1
            WHERE id = ?

        """, (reward, transition_id))

        self.connection.commit()

    def get_rl_transitions(self, resolved_only=True, limit=5000):

        cursor = self.connection.cursor()

        if resolved_only:
            cursor.execute("""
                SELECT * FROM rl_transitions
                WHERE resolved = 1
                ORDER BY id DESC LIMIT ?
            """, (limit,))
        else:
            cursor.execute("""
                SELECT * FROM rl_transitions
                ORDER BY id DESC LIMIT ?
            """, (limit,))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # VXLAN tunnel registry (used by telemetry/tunnel_manager.py to
    # reconcile the full-mesh overlay, and by telemetry/sla_collector.py
    # to know which switch pairs to monitor continuously).
    # ------------------------------------------------------------------

    def upsert_tunnel(self, switch_a, switch_b, vni,
                       port_a, port_b, remote_ip_a, remote_ip_b):

        cursor = self.connection.cursor()

        cursor.execute("""

            INSERT INTO tunnels(
                switch_a, switch_b, vni,
                port_a, port_b, remote_ip_a, remote_ip_b,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?)

            ON CONFLICT(switch_a, switch_b)

            DO UPDATE SET

                vni=excluded.vni,
                port_a=excluded.port_a,
                port_b=excluded.port_b,
                remote_ip_a=excluded.remote_ip_a,
                remote_ip_b=excluded.remote_ip_b

        """, (
            switch_a, switch_b, vni,
            port_a, port_b, remote_ip_a, remote_ip_b,
            time.time(),
        ))

        self.connection.commit()

    def get_tunnels(self):

        cursor = self.connection.cursor()

        cursor.execute("SELECT * FROM tunnels")

        rows = cursor.fetchall()

        return [dict(row) for row in rows]

    def delete_tunnel(self, switch_a, switch_b):

        cursor = self.connection.cursor()

        cursor.execute("""

            DELETE FROM tunnels
            WHERE switch_a=? AND switch_b=?

        """, (switch_a, switch_b))

        self.connection.commit()

    def clear_tunnels(self):


        cursor = self.connection.cursor()
        cursor.execute("DELETE FROM tunnels")
        self.connection.commit()