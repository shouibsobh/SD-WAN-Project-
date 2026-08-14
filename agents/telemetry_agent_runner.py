
import traceback

from agents.telemetry_agent import TelemetryAgent


def main():

    agent = TelemetryAgent()

    try:
        agent.run()
    except Exception:
        print("[telemetry_agent_runner] crashed:")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()