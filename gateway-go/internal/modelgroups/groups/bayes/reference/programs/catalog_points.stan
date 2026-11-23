// Generated from gateway-go/internal/bayescatalog/catalog.json by TestTheBayesProgramsAreTheCataloguesNames
// (gateway-go/internal/modelgroups/bayes_programs_test.go): do not edit.
// The self-test's check 7 (docs/specs/bayes-image.md, «The self-test»): Stan's own log-density (or log-mass) and log-CDF of
// every distribution of one scalar outcome, at the witness's points (reference/data/points.json, written as this program's
// data by the runner); run once with the fixed_param sampler.
data {
  int<lower=0> n_normal;
  array[n_normal, 2] real a_normal;
  array[n_normal] real x_normal;
  int<lower=0> n_student_t;
  array[n_student_t, 3] real a_student_t;
  array[n_student_t] real x_student_t;
  int<lower=0> n_cauchy;
  array[n_cauchy, 2] real a_cauchy;
  array[n_cauchy] real x_cauchy;
  int<lower=0> n_exponential;
  array[n_exponential, 1] real a_exponential;
  array[n_exponential] real x_exponential;
  int<lower=0> n_gamma;
  array[n_gamma, 2] real a_gamma;
  array[n_gamma] real x_gamma;
  int<lower=0> n_inv_gamma;
  array[n_inv_gamma, 2] real a_inv_gamma;
  array[n_inv_gamma] real x_inv_gamma;
  int<lower=0> n_lognormal;
  array[n_lognormal, 2] real a_lognormal;
  array[n_lognormal] real x_lognormal;
  int<lower=0> n_beta;
  array[n_beta, 2] real a_beta;
  array[n_beta] real x_beta;
  int<lower=0> n_uniform;
  array[n_uniform, 2] real a_uniform;
  array[n_uniform] real x_uniform;
  int<lower=0> n_std_normal;
  array[n_std_normal] real x_std_normal;
  int<lower=0> n_bernoulli_logit;
  array[n_bernoulli_logit, 1] real a_bernoulli_logit;
  array[n_bernoulli_logit] int x_bernoulli_logit;
  int<lower=0> n_binomial_logit;
  array[n_binomial_logit, 1] real a_binomial_logit;
  array[n_binomial_logit, 1] int i_binomial_logit;
  array[n_binomial_logit] int x_binomial_logit;
  int<lower=0> n_poisson_log;
  array[n_poisson_log, 1] real a_poisson_log;
  array[n_poisson_log] int x_poisson_log;
  int<lower=0> n_neg_binomial_2_log;
  array[n_neg_binomial_2_log, 2] real a_neg_binomial_2_log;
  array[n_neg_binomial_2_log] int x_neg_binomial_2_log;
}
generated quantities {
  array[n_normal] real lp_normal;
  array[n_normal] real lcdf_normal;
  array[n_student_t] real lp_student_t;
  array[n_student_t] real lcdf_student_t;
  array[n_cauchy] real lp_cauchy;
  array[n_cauchy] real lcdf_cauchy;
  array[n_exponential] real lp_exponential;
  array[n_exponential] real lcdf_exponential;
  array[n_gamma] real lp_gamma;
  array[n_gamma] real lcdf_gamma;
  array[n_inv_gamma] real lp_inv_gamma;
  array[n_inv_gamma] real lcdf_inv_gamma;
  array[n_lognormal] real lp_lognormal;
  array[n_lognormal] real lcdf_lognormal;
  array[n_beta] real lp_beta;
  array[n_beta] real lcdf_beta;
  array[n_uniform] real lp_uniform;
  array[n_uniform] real lcdf_uniform;
  array[n_std_normal] real lp_std_normal;
  array[n_std_normal] real lcdf_std_normal;
  array[n_bernoulli_logit] real lp_bernoulli_logit;
  array[n_binomial_logit] real lp_binomial_logit;
  array[n_poisson_log] real lp_poisson_log;
  array[n_neg_binomial_2_log] real lp_neg_binomial_2_log;
  for (i in 1:n_normal) {
    lp_normal[i] = normal_lpdf(x_normal[i] | a_normal[i, 1], a_normal[i, 2]);
    lcdf_normal[i] = normal_lcdf(x_normal[i] | a_normal[i, 1], a_normal[i, 2]);
  }
  for (i in 1:n_student_t) {
    lp_student_t[i] = student_t_lpdf(x_student_t[i] | a_student_t[i, 1], a_student_t[i, 2], a_student_t[i, 3]);
    lcdf_student_t[i] = student_t_lcdf(x_student_t[i] | a_student_t[i, 1], a_student_t[i, 2], a_student_t[i, 3]);
  }
  for (i in 1:n_cauchy) {
    lp_cauchy[i] = cauchy_lpdf(x_cauchy[i] | a_cauchy[i, 1], a_cauchy[i, 2]);
    lcdf_cauchy[i] = cauchy_lcdf(x_cauchy[i] | a_cauchy[i, 1], a_cauchy[i, 2]);
  }
  for (i in 1:n_exponential) {
    lp_exponential[i] = exponential_lpdf(x_exponential[i] | a_exponential[i, 1]);
    lcdf_exponential[i] = exponential_lcdf(x_exponential[i] | a_exponential[i, 1]);
  }
  for (i in 1:n_gamma) {
    lp_gamma[i] = gamma_lpdf(x_gamma[i] | a_gamma[i, 1], a_gamma[i, 2]);
    lcdf_gamma[i] = gamma_lcdf(x_gamma[i] | a_gamma[i, 1], a_gamma[i, 2]);
  }
  for (i in 1:n_inv_gamma) {
    lp_inv_gamma[i] = inv_gamma_lpdf(x_inv_gamma[i] | a_inv_gamma[i, 1], a_inv_gamma[i, 2]);
    lcdf_inv_gamma[i] = inv_gamma_lcdf(x_inv_gamma[i] | a_inv_gamma[i, 1], a_inv_gamma[i, 2]);
  }
  for (i in 1:n_lognormal) {
    lp_lognormal[i] = lognormal_lpdf(x_lognormal[i] | a_lognormal[i, 1], a_lognormal[i, 2]);
    lcdf_lognormal[i] = lognormal_lcdf(x_lognormal[i] | a_lognormal[i, 1], a_lognormal[i, 2]);
  }
  for (i in 1:n_beta) {
    lp_beta[i] = beta_lpdf(x_beta[i] | a_beta[i, 1], a_beta[i, 2]);
    lcdf_beta[i] = beta_lcdf(x_beta[i] | a_beta[i, 1], a_beta[i, 2]);
  }
  for (i in 1:n_uniform) {
    lp_uniform[i] = uniform_lpdf(x_uniform[i] | a_uniform[i, 1], a_uniform[i, 2]);
    lcdf_uniform[i] = uniform_lcdf(x_uniform[i] | a_uniform[i, 1], a_uniform[i, 2]);
  }
  for (i in 1:n_std_normal) {
    lp_std_normal[i] = std_normal_lpdf(x_std_normal[i]);
    lcdf_std_normal[i] = std_normal_lcdf(x_std_normal[i]);
  }
  for (i in 1:n_bernoulli_logit) {
    lp_bernoulli_logit[i] = bernoulli_logit_lpmf(x_bernoulli_logit[i] | a_bernoulli_logit[i, 1]);
  }
  for (i in 1:n_binomial_logit) {
    lp_binomial_logit[i] = binomial_logit_lpmf(x_binomial_logit[i] | i_binomial_logit[i, 1], a_binomial_logit[i, 1]);
  }
  for (i in 1:n_poisson_log) {
    lp_poisson_log[i] = poisson_log_lpmf(x_poisson_log[i] | a_poisson_log[i, 1]);
  }
  for (i in 1:n_neg_binomial_2_log) {
    lp_neg_binomial_2_log[i] = neg_binomial_2_log_lpmf(x_neg_binomial_2_log[i] | a_neg_binomial_2_log[i, 1], a_neg_binomial_2_log[i, 2]);
  }
}
