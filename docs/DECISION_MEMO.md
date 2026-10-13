# Week 6G Decision Memo

## 1. Structural infeasibility of the provisional torque criterion (Week 6E)

At torque balance, the coupled plant satisfies `T_avg == load + friction` to < 1e-6 N*m at every
declared load (nominal J_motor=0.0064, J_load=0.01, b=0.0008, Tf=0, r_scale=1.0; residual from
`sim/run_week6.py` per-window records, SEED=42). With a 30 % 2f ripple, the instantaneous minimum
is `T_min ~= 0.7 * T_avg`:

| load [N*m] | friction [N*m] | T_avg [N*m] | instantaneous min = 0.7*T_avg | required (0.9*load + 0.2) | attainable? |
|---|---|---|---|---|---|
| 6.0 | 0.246 | 6.246 | 4.372 | 5.600 | no |
| 10.0 | 0.243 | 10.243 | 7.170 | 9.200 | no |
| 14.0 | 0.239 | 14.239 | 9.967 | 12.800 | no |
| 17.0 | 0.236 | 17.236 | 12.065 | 15.500 | no |

Attainability requires `0.7*friction >= 0.2*load + 0.2`, false for all declared loads (friction ~0.24 => LHS ~0.17 vs RHS 1.4..3.6). The criterion is structurally unsatisfiable by throttle control at steady state (throttle sets speed/slip, not produced torque, which tracks demand at equilibrium). No replacement criterion is proposed here.

## 2. Savings scale check (committed runs; sensor floor from `results/metrics.json` Week 2)

Committed `week6c_stageA` (60 s, 600,000 samples, SEED=42) per-profile GT energy and savings. Sensor floor is
the Week 2 per-profile `p_mean_pct`/`p_max_pct` applied to each profile's legacy GT energy.

| profile | legacy GT [kWh] | MPC GT [kWh] | savings [kWh] | savings % of legacy | sensor floor mean [kWh] | sensor floor max [kWh] | |savings|/mean floor |
|---|---|---|---|---|---|---|---|
| steady | 0.058306 | 0.059221 | -0.000915 | -1.57 % | 0.000059 | 0.000124 | 15.5 |
| cyclic | 0.058622 | 0.058402 | +0.000219 | +0.37 % | 0.000060 | 0.000128 | 3.6 |
| bursty | 0.050090 | 0.050352 | -0.000262 | -0.52 % | 0.000051 | 0.000112 | 5.1 |

Sensor-level (0.101-0.223 % of energy): all three |savings| exceed the floor (1.7-15.5x). IPMVP-style estimation does not: baseline CV(RMSE)=7.65 % and estimator absolute errors 0.00021-0.00066 kWh, versus savings 0.000219-0.000915 kWh; the cyclic estimate has the wrong sign.

## 3. Potential-savings analysis (existing motor model, constant load, u in [0.4,1.0])

Minimum 1 s input energy over stable, in-slip-bound equilibria (r_scale=1.0). Ripple and slip limits as modeled.
No u satisfies the instantaneous 0.9*load+0.2 threshold (minima 4.372/7.170/9.967/12.065).

| load [N*m] | legacy input [W] at u=1.0 | best u | best input [W] | potential waste % | slip at best | instantaneous-min gap |
|---|---|---|---|---|---|---|
| 6.0 | 2161.9 | 0.70 | 2117.9 | 2.03 % | 0.0430 | 4.372 < 5.600 |
| 10.0 | 3481.1 | 0.90 | 3475.4 | 0.16 % | 0.0426 | 7.170 < 9.200 |
| 14.0 | 4837.4 | 1.00 | 4837.4 | 0.00 % | 0.0491 | 9.967 < 12.800 |
| 17.0 | 5885.4 | 1.00 | 5885.4 | 0.00 % | 0.0625 | 12.065 < 15.500 |

Ripple and slip constraints do not create large additional potential: torque at pull-out (s=0.15, u=1.0) is 27.45 N*m vs loads 6-17 (headroom ratio 4.57/2.74/1.96/1.61), slip margin to pull-out 0.130/0.116/0.101/0.088.

## 4. Torque-criterion options (NOT A PROPOSAL)

| candidate protected quantity | model can measure | model cannot measure | missing inputs | currently satisfied? |
|---|---|---|---|---|
| Instantaneous EM torque (`T_e(t)`) | `torque_Nm` (air-gap, 30 % ripple) | shaft torque; independent validation | physical shaft sensor; gearbox/link data | No on all 60/60? all 180 windows fail; 0/4 loads meet 0.9*L+0.2 |
| Average shaft torque | `T_avg = P_airgap/omega_sync` (air-gap) | actual shaft torque / friction-loss variation | shaft-power/ torque measurement, friction model validation | Steady-state identity T_avg = load + friction within <1e-6; quantity not independently available |
| Speed deviation (2f ripple) | `speed_rpm` traces, ripple pk-pk below | approved numeric limit; load disturbance response | product min speed, coupling, duty profile | Small oscillation only: nominal inertia (J=0.0064, J_load=0.01) pk-pk 3.54/5.79/8.01/9.66 rpm (0.118/0.193/0.267/0.322 % of 3000 rpm) at loads 6/10/14/17 |
| Stall margin | slip `s`, clamp flag, stable roots | approved stall threshold (speed/torque/current) | datasheet stall limits, protection curves, worst-case mechanical | All selected equilibrium roots stable at u=1; slip margins 0.130/0.116/0.101/0.088 to s_max=0.15 |
| Pull-out headroom | circuit peak torque at s=0.15 (27.45 N*m at u=1) | electrical/thermal pull-out envelope, hardware limits | motor datasheet, thermal data, over-current design | Headroom exists for all four loads (peak/load ratios 4.57/2.74/1.96/1.61) under the model |

Synthetic-inertia ripple (pk-pk rpm / % of 3000 rpm, load rows; J_motor columns 0.003 / 0.0064 / 0.015, J_load=0.01, b=0.0008, Tf=0):

| load | 0.003 | 0.0064 | 0.015 |
|---|---|---|---|
| 6 | 4.47 / 0.149 % | 3.54 / 0.118 % | 2.30 / 0.077 % |
| 10 | 7.32 / 0.244 % | 5.79 / 0.193 % | 3.77 / 0.126 % |
| 14 | 10.15 / 0.338 % | 8.01 / 0.267 % | 5.22 / 0.174 % |
| 17 | 12.24 / 0.408 % | 9.66 / 0.322 % | 6.29 / 0.210 % |

## 5. Open questions for the decision-maker

(a) Which physical quantity must be protected? Unblocks: which Week 6 diagnostics remain relevant and whether the provisional criterion is discarded or retained.  
(b) Real motor datasheet, rotor and load inertia, coupling (gear ratio)? Unblocks: replacement of the 0.003/0.0064/0.015 synthetic inertia grid and the 0.0004/0.0008/0.002 friction grid (currently labelled synthetic, not calibration).  
(c) Acceptable speed deviation, minimum speed, and approved margin with source? Unblocks: converting the ripple pk-pk numbers above into a pass/fail.  
(d) Is zero or negative ground-truth savings an acceptable terminal outcome? Current evidence is -0.000914666934483617 +0.000219205008172553 -0.000261985914848772 kWh (one of three positive). Unblocks: whether Week 6 is accepted against its energy objective.  
(e) Is an absolute-error criterion acceptable when GT savings are near zero, and at what threshold? Absolute errors are 0.00021166616053723653, 0.000023952215458746314, 0.0006576816085260218 kWh; the current rule leaves relative error undefined (null) for |GT| <= 1e-6 kWh.

No change to the provisional torque criterion, tolerances, <3 % target, or acceptance thresholds is made by this memo.
