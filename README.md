# Gemini Decisions for Home Assistant

Fills the **Decisions** slot under Settings > System > AI with Google's Gemini Decision 1 model. Any `ai_task.evaluate` call then gets calibrated probabilities back: yes/no, pick-one, and score questions.

Needs Home Assistant 2026.11 or later and a Gemini API key from Google AI Studio.

## Install

1. In Home Assistant, open **Marketplace** (or HACS), add this repository as a custom repository of type *Integration*, and install **Gemini Decisions**.
2. Restart Home Assistant.
3. Settings > Devices & services > Add integration > **Gemini Decisions**. Pick your existing Google Gemini key or paste one.
4. Settings > System > AI > **Decisions** > choose **Gemini Decisions**.

## Example

```yaml
action: ai_task.evaluate
data:
  task_name: front door
  state: "{{ states('binary_sensor.front_door') }} since {{ states.binary_sensor.front_door.last_changed }}"
  questions:
    left_open:
      type: noul
      instructions: Has the front door been left open by mistake?
response_variable: decision
continue_on_error: true
```

`decision.answers.left_open.noul` is the probability of yes (0.01 to 0.99).

## Safety rails

- **Caps:** 2 calls at a time, 12 per minute and 300 per day by default. Hitting the daily cap stops calls and raises a Repairs notice. Change them under the integration's options.
- **Attachments are off by default.** On Google's free tier, Google may use what you send to improve its products. Turn attachments on only if you're fine with that, or use a billing-enabled key.
- **Time limit:** each call gets one budget (30 s by default, longer for big attachments) and at most one retry, only for rate limits and Google outages.
- **The key** is only sent in a request header and is hidden from diagnostics and logs.
- Don't let these answers alone unlock doors or disarm alarms: text and images can try to argue for an answer.

## Cost

Input only, output is free. Free tier: no charge, up to 200 requests a minute and 10,000 a day. Paid: $0.10 per million input tokens (a few cents a month for typical home use).
