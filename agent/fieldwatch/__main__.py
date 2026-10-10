"""Local demo loop: python -m fieldwatch "north pivot" """
import sys
import uuid

from .investigation import reply, start


def main() -> None:
    field = sys.argv[1] if len(sys.argv) > 1 else "north pivot"
    inv = uuid.uuid4().hex[:8]
    print(f"== Investigating '{field}' (investigation {inv})")
    step = start(inv, field)
    while step.status == "waiting_for_farmer":
        print(f"\n[phone] {step.text}")
        answer = input("[farmer replies] ").strip() or "YES"
        step = reply(inv, step.interrupt_id, answer)
    print(f"\n[phone] {step.text}")
    if step.diagnosis:
        print("\n== Diagnosis object")
        print(step.diagnosis.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
