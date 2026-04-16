"""Setup Windows Task Scheduler for daily runs.

Creates a scheduled task that runs run_daily.bat at 6:00 AM daily.
Uses StartWhenAvailable so missed runs execute as soon as the PC wakes up.
Must be run with administrator privileges.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
BAT_PATH = PROJECT_ROOT / "scripts" / "run_daily.bat"

TASK_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>NFL News Agent — daily news collection and report generation</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{run_time}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{bat_path}</Command>
      <WorkingDirectory>{working_dir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def create_task(
    task_name: str = "NFL_News_Agent_Daily",
    run_time: str = "06:00",
):
    """Register a daily task in Windows Task Scheduler with missed-run catch-up."""
    xml_content = TASK_XML_TEMPLATE.format(
        run_time=run_time,
        bat_path=str(BAT_PATH),
        working_dir=str(PROJECT_ROOT),
    )

    # Write XML to a temp file and import it
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xml", delete=False, encoding="utf-16"
    ) as f:
        f.write(xml_content)
        xml_path = f.name

    cmd = [
        "schtasks", "/create",
        "/tn", task_name,
        "/xml", xml_path,
        "/f",  # Force overwrite if exists
    ]

    print(f"Creating scheduled task: {task_name}")
    print(f"  Script:  {BAT_PATH}")
    print(f"  Schedule: Daily at {run_time}")
    print(f"  Missed runs: Will execute on next wake/login")
    print()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print("Task created successfully!")
            print(result.stdout)
        else:
            print("Failed to create task:")
            print(result.stderr)
            if "Access is denied" in result.stderr:
                print("\nTry running this script as Administrator.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        Path(xml_path).unlink(missing_ok=True)


def delete_task(task_name: str = "NFL_News_Agent_Daily"):
    """Remove the scheduled task."""
    cmd = ["schtasks", "/delete", "/tn", task_name, "/f"]
    subprocess.run(cmd, capture_output=True, text=True)
    print(f"Deleted task: {task_name}")


def query_task(task_name: str = "NFL_News_Agent_Daily"):
    """Check if the scheduled task exists and its status."""
    cmd = ["schtasks", "/query", "/tn", task_name, "/fo", "LIST", "/v"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"Task '{task_name}' not found.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        action = sys.argv[1].lower()
        if action == "delete":
            delete_task()
        elif action == "status":
            query_task()
        elif action == "create":
            time_arg = sys.argv[2] if len(sys.argv) > 2 else "06:00"
            create_task(run_time=time_arg)
        else:
            print("Usage: python setup_scheduler.py [create|delete|status] [HH:MM]")
    else:
        create_task()
