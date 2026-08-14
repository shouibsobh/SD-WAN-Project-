import argparse
import os
import sys
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from chaos.fault_injector import FaultInjector


def main():

    parser = argparse.ArgumentParser(
        description="Inject a network fault for testing"
    )

    parser.add_argument(
        "--link", nargs=2, type=int, metavar=("SWITCH_A", "SWITCH_B"),
        help="Switch dpids on either end of the link, e.g. --link 1 2"
    )
    parser.add_argument("--delay", type=float, default=None,
                         help="Latency in ms")
    parser.add_argument("--jitter", type=float, default=None,
                         help="Jitter in ms (used together with --delay)")
    parser.add_argument("--loss", type=float, default=None,
                         help="Packet loss percent")
    parser.add_argument("--rate", type=float, default=None,
                         help="Bandwidth cap in Mbit/s")
    parser.add_argument("--duration", type=float, default=30,
                         help="Seconds before auto-clearing (default 30)")
    parser.add_argument("--down", action="store_true",
                         help="Hard link failure instead of netem impairment")
    parser.add_argument("--clear-all", action="store_true",
                         help="Clear netem on every discovered link and exit")

    args = parser.parse_args()

    injector = FaultInjector()

    if args.clear_all:
        injector.clear_all_faults()
        return

    if args.link is None:
        parser.error("--link SWITCH_A SWITCH_B is required "
                      "(unless using --clear-all)")

    switch_a, switch_b = args.link

    if args.down:

        injector.set_link_state(switch_a, switch_b, up=False)
        print(f"Link s{switch_a}<->s{switch_b} DOWN for {args.duration}s")

        time.sleep(args.duration)

        injector.set_link_state(switch_a, switch_b, up=True)
        print(f"Link s{switch_a}<->s{switch_b} restored")
        return

    injector.inject_temporary_fault(
        switch_a, switch_b,
        duration_s=args.duration,
        delay_ms=args.delay,
        jitter_ms=args.jitter,
        loss_percent=args.loss,
        rate_mbit=args.rate
    )


if __name__ == "__main__":
    main()
