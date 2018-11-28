pipeline {
  agent any
  environment {
    NMPI_TEST_USER            = "uhei-jenkins-test-user"
    NMPI_TEST_TOKEN           = credentials('NMPI_TESTUSER_TOKEN')
    NMPI_TEST_USER_NONMEMBER  = "uhei-jenkins-test-user"
    NMPI_TEST_TOKEN_NONMEMBER = credentials('NMPI_TESTUSER_TOKEN')
    NMPI_TEST_QUEUE           = "jenkins"
  }
  stages {
    stage('install-dependencies') {
      steps {
            sh 'singularity exec --app visionary-defaults /containers/stable/latest ./ci/install_dependencies_brainscales.sh'
      }
    }
    stage('test') {
      steps {
            sh 'singularity exec --app visionary-defaults /containers/stable/latest ./ci/run_saga_nosetests_brainscales.sh'
      }
    }
  }
}
