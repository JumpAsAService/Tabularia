data {
  int<lower=0> N;
  int<lower=1> J_dst;
  int<lower=1> J_sch;
  array[N] int<lower=1, upper=J_dst> d;
  array[N] int<lower=1, upper=J_sch> s;
  vector[N] y;
  array[J_sch] int<lower=1, upper=J_dst> Up_d_sch;
  int<lower=0, upper=1> Mode;
}
parameters {
  real<lower=0.0> s_hyper;
  real<lower=0.0> tau;
  vector<offset=0.0, multiplier=tau>[J_dst] mu_district;
  vector<lower=0.0>[J_sch] sigma_school;
}
model {
  s_hyper ~ exponential(1.0);
  tau ~ normal(0.0, s_hyper) T[0.0, ];
  mu_district ~ normal(0.0, tau);
  {
    vector[J_sch] Arg1_sigs = (mu_district[Up_d_sch]);
    for (I in 1:J_sch) {
      sigma_school[I] ~ normal(Arg1_sigs[I], s_hyper) T[0.0, ];
    }
  }
  y ~ normal((mu_district[d]), (sigma_school[s]));
}
