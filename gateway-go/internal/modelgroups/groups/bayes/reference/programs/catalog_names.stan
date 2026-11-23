// Generated from gateway-go/internal/bayescatalog/catalog.json by TestTheBayesProgramsAreTheCataloguesNames
// (gateway-go/internal/modelgroups/bayes_programs_test.go): do not edit.
// The self-test's check 6 (docs/specs/bayes-image.md, «The self-test»): every name of the catalogue's «Stan» column and every
// function of the expression language, called once, so that `stanc --info` reports each (only stanc reads this program).
data {
  real x;
  int y;
  int<lower=0> n;
  vector[2] v;
  cholesky_factor_corr[2] Lc;
  cholesky_factor_cov[2] Lv;
}
parameters {
  real theta;
}
model {
  theta ~ std_normal();
  // normal
  x ~ normal(x, x);
  target += normal_lpdf(x | x, x);
  target += normal_lcdf(x | x, x);
  target += normal_lccdf(x | x, x);
  // student_t
  x ~ student_t(x, x, x);
  target += student_t_lpdf(x | x, x, x);
  target += student_t_lcdf(x | x, x, x);
  target += student_t_lccdf(x | x, x, x);
  // cauchy
  x ~ cauchy(x, x);
  target += cauchy_lpdf(x | x, x);
  target += cauchy_lcdf(x | x, x);
  target += cauchy_lccdf(x | x, x);
  // exponential
  x ~ exponential(x);
  target += exponential_lpdf(x | x);
  target += exponential_lcdf(x | x);
  target += exponential_lccdf(x | x);
  // gamma
  x ~ gamma(x, x);
  target += gamma_lpdf(x | x, x);
  target += gamma_lcdf(x | x, x);
  target += gamma_lccdf(x | x, x);
  // inv_gamma
  x ~ inv_gamma(x, x);
  target += inv_gamma_lpdf(x | x, x);
  target += inv_gamma_lcdf(x | x, x);
  target += inv_gamma_lccdf(x | x, x);
  // lognormal
  x ~ lognormal(x, x);
  target += lognormal_lpdf(x | x, x);
  target += lognormal_lcdf(x | x, x);
  target += lognormal_lccdf(x | x, x);
  // beta
  x ~ beta(x, x);
  target += beta_lpdf(x | x, x);
  target += beta_lcdf(x | x, x);
  target += beta_lccdf(x | x, x);
  // uniform
  x ~ uniform(x, x);
  target += uniform_lpdf(x | x, x);
  target += uniform_lcdf(x | x, x);
  target += uniform_lccdf(x | x, x);
  // lkj_corr_cholesky
  Lc ~ lkj_corr_cholesky(x);
  target += lkj_corr_cholesky_lpdf(Lc | x);
  // multi_normal_cholesky
  v ~ multi_normal_cholesky(v, Lv);
  target += multi_normal_cholesky_lpdf(v | v, Lv);
  // std_normal
  x ~ std_normal();
  target += std_normal_lpdf(x);
  target += std_normal_lcdf(x);
  target += std_normal_lccdf(x);
  // bernoulli_logit
  y ~ bernoulli_logit(x);
  target += bernoulli_logit_lpmf(y | x);
  // binomial_logit
  y ~ binomial_logit(n, x);
  target += binomial_logit_lpmf(y | n, x);
  // poisson_log
  y ~ poisson_log(x);
  target += poisson_log_lpmf(y | x);
  // neg_binomial_2_log
  y ~ neg_binomial_2_log(x, x);
  target += neg_binomial_2_log_lpmf(y | x, x);
}
generated quantities {
  real draw_normal = normal_rng(x, x);
  real draw_student_t = student_t_rng(x, x, x);
  real draw_cauchy = cauchy_rng(x, x);
  real draw_exponential = exponential_rng(x);
  real draw_gamma = gamma_rng(x, x);
  real draw_inv_gamma = inv_gamma_rng(x, x);
  real draw_lognormal = lognormal_rng(x, x);
  real draw_beta = beta_rng(x, x);
  real draw_uniform = uniform_rng(x, x);
  matrix[2, 2] draw_lkj_corr_cholesky = lkj_corr_cholesky_rng(2, x);
  vector[2] draw_multi_normal_cholesky = multi_normal_cholesky_rng(v, Lv);
  real draw_std_normal = std_normal_rng();
  int draw_bernoulli_logit = bernoulli_logit_rng(x);
  int draw_binomial_logit = binomial_rng(n, inv_logit(x));
  int draw_poisson_log = poisson_log_rng(x);
  int draw_neg_binomial_2_log = neg_binomial_2_log_rng(x, x);
  real function_exp = exp(x);
  real function_log = log(x);
  real function_log1p = log1p(x);
  real function_expm1 = expm1(x);
  real function_sqrt = sqrt(x);
  real function_square = square(x);
  real function_inv_logit = inv_logit(x);
  real function_logit = logit(x);
  real function_log1p_exp = log1p_exp(x);
  real function_log_inv_logit = log_inv_logit(x);
}
