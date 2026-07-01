function dxdt = eomHCW(t, x, n)
%% Equations of Motion for HCW

% initialize input
u = zeros(3,1);

% HCW方程式の状態方程式
A = [zeros(3,3), eye(3)
    3*n^2, 0, 0, 0, 2*n, 0
    0, 0, 0, -2*n, 0, 0
    0, 0, -n^2, 0, 0, 0];

B= [zeros(3,3)
    eye(3)];

dxdt = A*x + B*u;

end