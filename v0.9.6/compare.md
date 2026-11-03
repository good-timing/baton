## python: 0.9.5 published vs 0.9.6 checkout

The numbers compare two versions of one SDK on one machine. They are not what a vendor sees in production: the collector is local and the client is in the same process. Median (lowest–highest) of 5, 5 runs.

| measure | metric | 0.9.5 published | 0.9.6 checkout | change |
|---|---|---|---|---|
| overhead | plain_p50_ms | 0.467 (0.459–0.491) | 0.473 (0.463–0.49) | +1% (noise) |
| overhead | plain_p99_ms | 0.55 (0.525–0.688) | 0.562 (0.532–0.573) | +2% (noise) |
| overhead | baton_p50_ms | 0.986 (0.866–1.02) | 0.902 (0.872–0.959) | -8% (noise) |
| overhead | baton_p99_ms | 1.62 (1.45–1.71) | 1.59 (1.54–1.75) | -2% (noise) |
| overhead | added_p50_ms | 0.524 (0.399–0.559) | 0.43 (0.405–0.469) | -18% (noise) |
| overhead | added_p99_ms | 1.08 (0.896–1.12) | 1.04 (1–1.18) | -3% (noise) |
| burst | wall_ms | 8,982 (8,906–9,002) | 9,082 (8,991–9,137) | +1% |
| burst | call_p50_ms | 8.75 (8.69–8.77) | 8.87 (8.75–8.9) | +1% |
| burst | call_p99_ms | 13.9 (13.7–14.2) | 14.1 (13.8–14.2) | +1% (noise) |
| burst | close_ms | 1.85 (1.71–2.02) | 2.13 (1.64–3.34) | +15% |
| burst | emitted | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | delivered | 20,000 (20,000–20,000) | 20,000 (20,000–20,000) | same |
| burst | lost | 0 (0–0) | 0 (0–0) | same |
| outage_hang | wall_ms | 1,543 (1,519–1,570) | 1,551 (1,535–1,552) | +0% (noise) |
| outage_hang | call_p50_ms | 7.44 (7.35–7.61) | 7.48 (7.41–7.55) | +0% (noise) |
| outage_hang | call_p99_ms | 13 (12.3–13.7) | 13 (12.8–13.7) | -0% (noise) |
| outage_hang | close_ms | 3,005 (3,005–3,006) | 3,005 (3,005–3,006) | -0% (noise) |
| outage_hang | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_hang | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_hang | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | wall_ms | 1,583 (1,562–1,672) | 1,604 (1,576–1,691) | +1% (noise) |
| outage_down | call_p50_ms | 7.68 (7.61–7.84) | 7.78 (7.6–8.08) | +1% (noise) |
| outage_down | call_p99_ms | 13.1 (12.6–15.1) | 13.3 (12.9–14.8) | +1% (noise) |
| outage_down | close_ms | 706 (706–707) | 706 (706–706) | +0% (noise) |
| outage_down | emitted | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| outage_down | delivered | 0 (0–0) | 0 (0–0) | same |
| outage_down | lost | 4,000 (4,000–4,000) | 4,000 (4,000–4,000) | same |
| memory | heap_after_warmup_bytes | 605,736 (599,629–621,663) | 618,687 (609,086–630,452) | +2% (noise) |
| memory | heap_at_half_bytes | 620,006 (613,807–624,471) | 627,595 (623,700–641,181) | +1% |
| memory | heap_at_end_bytes | 620,001 (613,229–625,414) | 631,626 (572,994–642,365) | +2% |
| memory | growth_per_call_bytes | 0.0653 (-0.887–0.136) | 0.0197 (-5.46–1.72) | -0.0456 (noise) |
| memory_outage | heap_after_warmup_bytes | 1,496,205 (1,487,770–1,498,070) | 1,503,017 (1,501,696–1,505,083) | +0% |
| memory_outage | heap_at_half_bytes | 3,611,970 (3,606,428–3,622,800) | 3,624,229 (3,623,405–3,632,892) | +0% |
| memory_outage | heap_at_end_bytes | 3,623,713 (3,619,572–3,629,093) | 3,542,222 (3,541,169–3,629,274) | -2% |
| memory_outage | growth_per_call_bytes | 1.25 (-0.322–2.27) | -8.13 (-9.17–0.504) | -9.38 |
| process | max_rss_bytes | 87,388,160 (87,355,392–92,999,680) | 88,174,592 (88,023,040–92,446,720) | +1% (noise) |
