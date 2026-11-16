# myEnergy interval-data request

Observed in an authenticated browser session. This note describes one request that returned interval readings. All identifiers, session fields, dates, and readings in examples below are placeholders or invented; no raw capture was saved to the repository.

## Reproduce the request

1. Sign in to myEnergy yourself and open **Energy usage** from the dashboard/navigation.
2. In browser Network tools, filter to Fetch/XHR and look for a `POST` to `/s/sfsites/aura` whose form-encoded `message` calls `EnergyInformationController.getMeterData` with `formatType: "Customer Access NEM12"`.
3. Inspect the response's `actions[0].returnValue.returnValue.data.body.nmiData` string. It contains NEM12 comma-separated records.

In this session, entering **Energy usage** triggered the data calls. Changing **Day**, **Previous**, and **Year** after the page loaded did not trigger another request; those controls appeared to use data already fetched. To reproduce the observed call, open or reload the Energy usage page, rather than relying on a graph control to fetch again. This is an observation of one session, not a guarantee about every date range.

## Observed request

| Item | Observed shape |
| --- | --- |
| Origin and path | `https://myenergy.powercor.com.au/s/sfsites/aura` |
| HTTP method | `POST` |
| Query parameter names | `r`, `aura.ApexAction.execute`; values omitted |
| Request content type | `application/x-www-form-urlencoded; charset=UTF-8` |
| Form field names | `message`, `aura.context`, `aura.pageURI`, `aura.token` |
| Action | `aura://ApexActionController/ACTION$execute` → `EnergyInformationController.getMeterData` |
| Action parameters | `nmi` (string), `formatType` (string), `startDate` and `endDate` (`YYYY-MM-DD` strings), `company` (string), `jobId` (number) |
| Observed `formatType` | `Customer Access NEM12` |
| Authentication | Existing browser session; cookie and Aura token values omitted |
| Response | HTTP 200, `application/json` |

The observed date bounds matched the first and last `300` record dates, inclusive. The response covered multiple dates; the maximum supported range was not established. Other page-load calls used the same action and one used `MDPP Interval Summary`; this note documents only the NEM12 call.

### Sanitised request shape

This shows the useful part of the form-encoded `message` field. Framework context, page URI, and token are intentionally omitted. The dates and identifier are invented; this is not a replayable request.

```json
{
  "actions": [
    {
      "descriptor": "aura://ApexActionController/ACTION$execute",
      "params": {
        "classname": "EnergyInformationController",
        "method": "getMeterData",
        "params": {
          "nmi": "<NMI>",
          "formatType": "Customer Access NEM12",
          "startDate": "2026-01-01",
          "endDate": "2026-01-02",
          "company": "<company>",
          "jobId": 0
        }
      }
    }
  ]
}
```

## Observed response

The returned JSON has one action with `state: "SUCCESS"`. The useful path is `actions[0].returnValue.returnValue.data.body`, containing `formatType`, `jobId`, `nmi`, `nmiData`, and `status: "Success"`; `isSuccess` is a sibling of `data`. The response also contains Salesforce context and performance fields, omitted here.

```json
{
  "actions": [
    {
      "state": "SUCCESS",
      "returnValue": {
        "returnValue": {
          "data": {
            "body": {
              "formatType": "Customer Access NEM12",
              "jobId": 0,
              "nmi": "<NMI>",
              "nmiData": "<NEM12 text; record layout below>",
              "status": "Success"
            }
          },
          "isSuccess": true
        }
      }
    }
  ]
}
```

`nmiData` is a **string**, not a JSON interval array. The observed layout uses a `100` header, `200` stream headers, `300` daily interval rows, optional `400` interval event rows, and a `900` end marker. The structural excerpt below deliberately substitutes placeholders for identifying fields and readings:

```text
100,NEM12,<file-created-time>,<from-participant>,<to-participant>
200,<NMI>,<configuration>,<register-id>,B1,<stream-id>,<meter-serial>,KWH,30,
300,<YYYYMMDD>,<48 kWh interval values>,<quality-method>,<reason-code>,<reason-description>,<YYYYMMDDhhmmss>,
[optional 400 interval event rows]
200,<NMI>,<configuration>,<register-id>,E1,<stream-id>,<meter-serial>,KWH,30,
300,<YYYYMMDD>,<48 kWh interval values>,<quality-method>,<reason-code>,<reason-description>,<YYYYMMDDhhmmss>,
900
```

The observed `200` records declared `KWH` and 30-minute intervals, giving 48 values per source day. The examples retain the `B1` and `E1` suffixes needed to explain direction; controlled-load behaviour was not established. AEMO's direction convention is market-centric: `E1` is energy **exported from the market to the property** (property usage), while `B1` is energy **imported into the market from the property** (property export). See the [AEMO NEM12 format specification](https://aemo.com.au/-/media/files/stakeholder_consultation/consultations/nem-consultations/2024/2024-metering-services-review-package-1/final-stage/mdff-specification-nem12-nem13-v27.pdf) and [NMI direction/suffix procedure](https://www.aemo.com.au/-/media/files/electricity/nem/retail_and_metering/metering-procedures/2026/msats-national-metering-identifier-procedure-v74.pdf).

Each `300` row gives an `IntervalDate` in `YYYYMMDD` format followed by 48 ordered values. It does **not** give a timestamp or UTC offset per value. Under the AEMO NEM12 convention, value 1 is the period ending 00:30 on that date, and value 48 is the period ending 00:00 on the next date. The observed dates did not include a daylight-saving transition, so that capture alone did not verify timezone or transitions. The subsequent
[client check](client.md) documents the AEST interpretation and transition-day evidence. In this capture, the `100` header's file creation time used 12 digits (`YYYYMMDDhhmm`). Every `300` row had a 14-digit `UpdateDateTime` (`YYYYMMDDhhmmss`), while `MSATSLoadDateTime` was empty. These metadata fields do not timestamp individual intervals and provide no UTC offset. `400` rows can describe interval-level events.

## Safe handling

Never commit or paste a raw HAR, copied cURL/fetch request, cookie, Aura token, NMI, account ID, meter serial, address, or actual reading. The form field `aura.token`, framework context, query values, and response `nmiData` can contain sensitive material. Keep any temporary raw capture outside the repository and delete it after deriving a sanitised shape. `.gitignore` is only a backup guardrail.

When comparing another capture, record field names/types and channel/interval semantics, then replace identifiers and readings with placeholders or invented values before sharing. Do not assume every property has the same streams or 30-minute data.
