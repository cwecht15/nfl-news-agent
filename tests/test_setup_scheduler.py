"""scripts/setup_scheduler.py — task XML for the local dispatch tasks."""

import sys
import xml.dom.minidom
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import setup_scheduler as ss  # noqa: E402


def _parse(xml_text):
    return xml.dom.minidom.parseString(xml_text.replace('encoding="UTF-16"', ""))


def test_single_trigger_tasks_keep_their_schedule():
    doc = _parse(ss.task_xml("16:15", ss.ELEVATIONS_BAT, "d", "PT15M", "Saturday"))
    triggers = doc.getElementsByTagName("CalendarTrigger")
    assert len(triggers) == 1
    assert triggers[0].getElementsByTagName("StartBoundary")[0].firstChild.data == "2026-01-01T16:15:00"
    assert triggers[0].getElementsByTagName("Saturday")
    assert not doc.getElementsByTagName("Repetition")
    daily = _parse(ss.task_xml("06:00", ss.DAILY_BAT, "d", "PT2H"))
    assert daily.getElementsByTagName("ScheduleByDay")


def test_injuries_task_fires_wed_to_sat_and_repeats_friday():
    doc = _parse(ss.task_xml("00:00", ss.INJURIES_BAT, "d", "PT15M", triggers=ss.INJURY_TRIGGERS))
    triggers = doc.getElementsByTagName("CalendarTrigger")
    days = [next(n.tagName for n in t.getElementsByTagName("DaysOfWeek")[0].childNodes if n.nodeType == n.ELEMENT_NODE)
            for t in triggers]
    assert days == ["Wednesday", "Thursday", "Friday", "Saturday"]
    friday = triggers[2]
    rep = friday.getElementsByTagName("Repetition")[0]
    assert rep.getElementsByTagName("Interval")[0].firstChild.data == "PT45M"
    assert rep.getElementsByTagName("Duration")[0].firstChild.data == "PT3H"
    assert len(doc.getElementsByTagName("Repetition")) == 1
    assert doc.getElementsByTagName("Command")[0].firstChild.data.endswith("run_injuries.bat")
