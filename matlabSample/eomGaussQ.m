%[text] # Equations of motion for attitude and orbit
%[text] 姿勢・軌道計算のための運動方程式
%[text] ## state variables
%[text] ${\\bf x}=\[a,e,i,\\Omega, w, f, {\\bf q}^T, {\\bf \\omega}^T\]^T$
%[text] `a`: semi-major axis, km
%[text] `e`: eccentricity
%[text] `inc`: inclination, rad
%[text] `raan`: right ascension of ascending node, rad
%[text] `ome`: argument of perigee, rad
%[text] `f`: true anomaly, rad
%[text] `q`: quaternion where q(4) is the scalar part
%[text] `w`: angular rate, rad/s
%[text] ## note
%[text] NA
%[text] ## references
%[text] NA
%[text] ## revisions
%[text] 20210209  y.yoshimura, y.yoshimula@gmail.com
%[text] See also orbitConst, mainATOM.
function dxdt = eomGaussQ(t_, x_, para, sat, const, EGM, earthVSOP, coefs, ELP, EOP)
%[text] ## pre-allocation
% pre-allocation
aRTN = zeros(3,1); % translational acceleration at RSW frame
trq = zeros(3,1); % control torque at body-fixed frame

anomalyFlag = para.anomalyFlag;
jd = para.jd + s2day(t_);
%[text] ## state variables
%[text] ### orbit
a = x_(1); % semi-major axis, km
e = x_(2); % eccentricty
inc = x_(3); % inclination, rad
raan = x_(4); % right ascension of ascending node, rad
ome = x_(5); % argument of perigee, rad
if anomalyFlag == 1
    f = x_(6); % true anomaly, rad
else
    f = trueAnomaly(a, e, x_(6));
end

p = a * (1 - e^2); % semi-latus rectum
h = sqrt(const.GE * p); % orbital angular momentum
n = sqrt(const.GE / a^3); % mean motion
u = ome + f; % argument of latitude
r = p / (1.0 + e * cos(f));

[rVec, ~] = oe2rv([a, e, inc, raan, ome ,f], 1, const.GE); % km, km/s
rVec = rVec(:);

%[text] ### attitude
qNorm = norm(x_(7:10));
q1 = x_(7) / qNorm; % quaternion, vector part
q2 = x_(8) / qNorm;
q3 = x_(9) / qNorm;
q4 = x_(10) / qNorm; % scalar part
qv = [q1; q2; q3]; % vector part
q = [qv; q4];
wx = x_(11); % angular rate, rad/s
wy = x_(12);
wz = x_(13);
wVec = [wx; wy; wz];

%[text] ### longitude, latitude
rECEF = zyx2dcm(gmst(jd), 0, 0) * rVec; %km, position at ECEF frame, 3x1
lon = atan2(rECEF(2), rECEF(1)); % rad, longitude
lat = atan(rECEF(3) / norm(rECEF(1:2))); % rad, latitude
[geoLon, geoLat, geoH] = geocentric2Geodetic(rVec(1), rVec(2), rVec(3), const.RE, const.fE);
%[text] ## DCM
Rbi = q2dcm(4, q');
RiECEF = itrf2gcrf(jd, EOP); % dcm from ECEF to inertial frame (GCRF)
Roi = zxz2dcm(raan, inc, u); % dcm from inertial to RTN frame
%[text] ## Earth's gravitational potential
% dcmNutation = nutationDCM(jd,const);% nutation
% dcmPrecession = precessionDCM(const.J2000, jd, const); % precession
% tmpDCM = dcmNutation * dcmPrecession;
% rsw2TOD = tmpDCM * Roi';
% tod2RSW = rsw2TOD';

tmp = egm2008(rECEF', EGM.GEODEG, EGM.Cnm, EGM.Snm, const); % at Cartesian coordinate
fEarth = RiECEF * tmp(:);
aRTN = aRTN + Roi * fEarth;
%[text] ## Sun's gravitational force
[aSunI, sunI] = sunG(jd, rVec', const, earthVSOP);
aSunI = aSunI(:);
sunI = sunI(:); % sun position at inertial frame, km, 3x1
aSun = Roi * aSunI; % acceleration by sun
aRTN = aRTN + aSun;
%[text] ## Moon's gravitational force
[aMoon, ~] = moonG(jd, rVec', const, ELP);
aMoon = aMoon(:);
aMoon = Roi * aMoon; % acceleration by moon
aRTN = aRTN + aMoon;

%[text] ## SRP
sunRelI = sunI - rVec; % 3x1 vector, from sat to sun vector at IJK, km
sunDist = norm(sunRelI);
sunRelB = Rbi * sunRelI; % km, at body-fixed frame
sat = srpSimple(sat, sunRelB./norm(sunRelB), sunDist.*10^3, const); % SRP
% sat = selfShadow(sat, sunRelB./norm(sunRelB)); % calc self-shadowing
fSRP = sum(sat.force,1)'; % N, 3x1 vector at body-fixed frame
tSRP = sum(sat.torque,1)'; % Nm, 3x1 vector at body-fixed frame

aRTN = aRTN + Roi * Rbi' * fSRP ./ sat.m ./ 10^3;
trq = trq + tSRP;
%[text] ## air drag
% Cd = 2.2;
% [~, rho] = jacciaBowman(jd, geoLon, geoLat, geoH, const, JB);
% vRel = [h / e * sin(f) / p
%     h / r - const.WE * r * ijk2RSW(3,3)
%     const.WE * r * ijk2RSW(2,3)]; % relative velocity at RSW
% vRel = ijk2B * ijk2RSW' * vRel; % relative velocity at body-fixed
% tmp = sat.normal * (vRel ./ norm(vRel));
% ampF = -0.5 .* Cd .* sat.area .* tmp ./ sat.m .* rho .* norm(vRel).^2; % scalar, at body-fixed
% ampF = ampF .* (tmp > 0); % Nx1
% fAirB = ampF .* (vRel ./ norm(vRel))'; % Nx3 matrix for each facet
% tAir = cross(sat.pos, fAirB); % Nx3 matrix for each facet
% tAir = sum(tAir, 1)'; % 3x1 vector at body-fixed
% fAirB = sum(fAirB, 1); % 1x3 vector at body-fixed
% fAir = ijk2RSW * ijk2B' * fAirB'; % 3x1 air drag at RSW
%
% aRSW = aRSW + fAir;
% trq = trq + tAir;
%[text] ## gravitational gradient torque
%[text] earth directional (unit) vector, 3x1 vector at body-fixed frame
nGG = Rbi * (-rVec./norm(rVec));
tGG = 3 * const.GEm / (r*10^3)^3 * cross(nGG, sat.J*nGG); % Nm

trq = trq + tGG;
%[text] ## remanent magnetic torques
% bNED = geodeticIGRF(jd, geoLat, geoLon, geoH, coefs)'; % 3x1, nT, at NED frame
% bI = RiECEF' * ecef2NED' * bNED; % nT< at ECI frame
% bB = Rbi * bI .* 10^(-9); % 3x1 vector, T, at body-fixed frame
% trq_mag = cross(sat.mag, bB); % Nm
% trq = trq + trq_mag;


%[text] ## Equations of motion
%[text] Gauss
% Gauss
dOmedt = r * sin(u) / n / a^2 / sqrt(1 - e^2) / sin(inc) * aRTN(3);
domedt = -sqrt(1 - e^2) / n / a / e * (cos(f) * aRTN(1) - (sin(f) + sin(f) / (1 + e * cos(f))) * aRTN(2)) - dOmedt * cos(inc);

if anomalyFlag == 1    %  true anomaly used,   dfdt
    tmp = h / r^2  - domedt - dOmedt * cos(inc);
else % mean anomaly used, dmdt
    tmp = n + 1 / n / a^2 / e * ((p * cos(f) - 2 * e * r) * aRTN(1) ...
        - (p + r) * sin(f) * aRTN(2));
end

Gauss = [2 / n / sqrt(1 - e^2) * (e * sin(f) * aRTN(1) + (1 + e * cos(f)) * aRTN(2))
    sqrt(1 - e^(2)) / n / a * (sin(f) * aRTN(1) + (cos(f) + (e + cos(f)) / (1 + e * cos(f))) * aRTN(2))
    r * cos(u) / (n * a^2 * sqrt(1 - e^2)) * aRTN(3)
    dOmedt
    domedt
    tmp];

% rotational motion
rotDynamics = -sat.MOI^(-1) * cross(wVec, (sat.MOI * wVec)) + sat.MOI^(-1) * trq;
qKinematics = qKine(4, q', wVec')';

% do not rewrite the script below
%------->state variables [x, y, z, vx, vy, vz, q0, q1, q2, q3, wx, wy, wz]
dxdt = [Gauss
    qKinematics
    rotDynamics];

end

%[appendix]{"version":"1.0"}
%---
