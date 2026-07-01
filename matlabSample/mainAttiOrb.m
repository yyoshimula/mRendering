%[text] # Coupled Attitude and Orbit Motion
%[text] ## note
%[text] ## references 
%[text] ## revisions
%[text] ©2022  y.yoshimura, y.yoshimula@gmail.com, y.yoshimura.a64@m.kyushu-u.ac.jp
clc
clear
close all

tic
%%
%[text] ## satellite and environment parameters
const = orbitConst;
earthVSOP = vsopConst;

%[text] ### Earth gravity model (EGM2008)
% read EGM2008 coefficients
EGM.GEODEG = 8; % geoid degree %[control:editfield:3057]{"position":[14,15]}
EGM = readEGM2008(EGM, EGM.GEODEG);

% for ITRF and GCRF
EOP = readEOP();

% read ELP coefficients for Moon
ELP = readELP();

% read IGRF coeficients
% coefsという構造体に係数が保存されている
load('igrfcoefs.mat', 'coefs');

%[text] ### Atmospheric drag (Jaccia–Bowman 2008)
% read coefficients for Jaccia–Bowman 2008
% SAT_Const % for JB20089 calculation %[output:999440a2]
% constants % for JB20089 calculation
% [JB.PC, JB.EOPdata, JB.SOLdata, JB.DTCdata] = readJB2008;

%[text] ### Satellite model
sat = readSC('boxWing.obj'); % satellite shape and optical parameters
nFacet = size(sat.faces,1);
sat.J = diag([32.625 73.95 79.935]); % moment of inertia, kgm^2 %[control:editfield:5ea1]{"position":[9,36]}
sat.m = 150; % mass, kg %[control:editfield:9822]{"position":[9,12]}
sat.mag = [0.1; 0.2; 0.3]; % Am^2, remanent magnetism %[control:editfield:972b]{"position":[11,26]}
sat.kappa = zeros(nFacet,1);
sat.Cd = 0.5 .* ones(nFacet, 1);
sat.Cs = 0.5 .* ones(nFacet, 1);
sat.Ca = 0.0 .* ones(nFacet, 1);


showSC(sat) % visualize satellite model
%%
%[text] ## initial condition
%[text] ### attitude
qIni = [0 0 0 1]; % initial quaternion, q(4) is the scalar part
wIni = [deg2rad(5), deg2rad(3), deg2rad(5)]; % initial angular rate, rad/s
%[text] ### orbit
%[text] tle.oe = $a {\\rm \[km\]}, e,i {\\rm \[rad\]},\\Omega {\\rm \[rad\]},\\omega{\\rm \[rad\]},M{\\rm \[rad\]}$
tle = readTLE('ideaOrbit.txt', const);
% osculating orbital elements
% osc := [a_osc,  e_osc, i_osc, Ome_osc, w_osc, f_osc, M_osc, r_osc, dr_osc, p_osc, u_osc];
tmp = mean2Osc(tle.n, tle.oe(:,2),tle.oe(:,3),tle.oe(:,4),tle.oe(:,5),tle.oe(:,6), const);
[iJ2000, OmeJ2000, wJ2000] = teme2J2000(tle.jd, tmp(3), tmp(4), tmp(5), const);
oeIni = [tmp(1:2), iJ2000, OmeJ2000, wJ2000, tmp(6)]; % [a, e, inc, RAAN, w, f]
%[text] ### time and state vector
%[text] `x_ini :=` $\[a {\\rm \[km\]}, e,i {\\rm \[rad\]},\\Omega {\\rm \[rad\]},w{\\rm \[rad\]}, f{\\rm \[rad\]}, \\bf{q}^T, \\bf{\\omega}^T\]^T$
%[text] `a:` semi-major axis, km
%[text] `e:` eccentricity
%[text] `inc:` inclination, rad
%[text] `RAAN:` right ascension of the ascending node
%[text] `ome:` argument of periapsis, rad
%[text] `f:` true anomaly, rad
%[text] `q:` quaternion, w.r.t. inertial frame, q4 is the scalar part
%[text] `w:` angular rate w.r.t. inertial frame, rad/s

para.jd = tle.jd; % Julian day, day
para.anomalyFlag = 1; % flag for true anomaly

tspan = 0:1:1*360; % time span, s
% tspan = [0 60];

xIni = [oeIni, qIni, wIni];
%%
%[text] ## solve ODE
option = odeset('Reltol',1e-10,'AbsTol',1e-10);
[tOut, yOut] = ode45(@(t,x)eomGaussQ(t,x, para, sat, const, EGM, earthVSOP, coefs, ELP, EOP), tspan, xIni, option);
%%
%[text] ## result
time = tOut; % elapsed time, s
a = yOut(:,1); % semimajor axis, km
e = yOut(:,2); % eccentricity
inc = yOut(:,3); % inclination, rad
raan = yOut(:,4); % right ascension of the ascending node, rad
ome = yOut(:,5); % argument of periapsis, rad
f = yOut(:,6); % true anomaly, rad
q1 = yOut(:,7); % quaternion w.r.t. inertial frame
q2 = yOut(:,8);
q3 = yOut(:,9);
q4 = yOut(:,10);
q = [q1 q2 q3 q4];
q = q ./ vecnorm(q, 2, 2); % normalize before CSV export
q1 = q(:,1);
q2 = q(:,2);
q3 = q(:,3);
q4 = q(:,4);
wx = yOut(:,11); % angular rate w.r.t. inertial frame, rad/s
wy = yOut(:,12);
wz = yOut(:,13);

% position and veloctiy at Cartesian, km, km/s 
[r, v] = oe2rv([a, e, inc, raan, ome, f], 1, const.GE); 

%% save to csv
oeTable = table(time, a, e, inc, raan, ome, f, q1, q2, q3, q4, ...
    'VariableNames', {'time_s', 'a_km', 'e', 'inc_rad', 'raan_rad', 'ome_rad', 'f_rad', ...
                      'q1', 'q2', 'q3', 'q4'});
writetable(oeTable, 'attitudeOrbit.csv');
fprintf('Saved orbit and quaternion to attitudeOrbit.csv\n');
%%
%[text] ## show figs
figure
plot3(r(:,1), r(:,2), r(:,3))

figure
tiledlayout(3,1)
nexttile
plot(time, rad2deg(wx))
nexttile
plot(time, rad2deg(wy))
nexttile
plot(time, rad2deg(wz))
xlabel('time [min]')
ylabel('angular rate [deg/s]')
figure
tiledlayout(4,1), nexttile
plot(time, q1), nexttile
plot(time, q2), nexttile
plot(time, q3), nexttile
plot(time, q4)

toc

%[appendix]{"version":"1.0"}
%---
%[metadata:view]
%   data: {"layout":"inline","rightPanelPercent":32.1}
%---
%[control:editfield:3057]
%   data: {"defaultValue":"8","label":"geoid degree","run":"Nothing","valueType":"MATLAB code"}
%---
%[control:editfield:5ea1]
%   data: {"defaultValue":"diag([32.625 73.95 79.935])","label":"moement of inertia","run":"Nothing","valueType":"MATLAB code"}
%---
%[control:editfield:9822]
%   data: {"defaultValue":"150","label":"satellite mass","run":"Nothing","valueType":"MATLAB code"}
%---
%[control:editfield:972b]
%   data: {"defaultValue":"[0.1; 0.2; 0.3]","label":"remanent mag","run":"Nothing","valueType":"MATLAB code"}
%---
%[output:999440a2]
%   data: {"dataType":"error","outputData":{"errorType":"runtime","text":"関数または変数 'SAT_Const' が認識されません。"}}
%---
