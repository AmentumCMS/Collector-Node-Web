# -*- mode: ruby -*-
# vi: set ft=ruby :

# All Vagrant configuration is done below.
Vagrant.configure("2") do |config|
  # The most common configuration options are documented and commented below.
  # For a complete reference, please see the online documentation at
  # https://docs.vagrantup.com.

  # Every Vagrant development environment requires a box. You can search for
  # boxes at https://vagrantcloud.com/search.
  config.vm.box = "oraclelinux/9-btrfs"
  config.vm.box_url = "https://oracle.github.io/vagrant-projects/boxes/oraclelinux/9-btrfs.json"
  config.vm.hostname = "Node-Collector"

  config.ssh.key_type = :ecdsa521 # Requires Vagrant 2.4.1

  ############################################################################
  # Provider-specific configuration                                          #
  ############################################################################
  config.vm.provider "virtualbox" do |vb|
    # Set Name
    vb.name = "Node Collector - OEL9"

    # Display the VirtualBox GUI when booting the machine
    vb.gui = false
    
    # Set up VM options
    vb.customize ["modifyvm", :id, "--vm-process-priority", "normal"]
    vb.customize ["modifyvm", :id, "--graphiccontroller", "vmsvga"]
    vb.customize ["modifyvm", :id, "--vram", "160"]
    vb.customize ["modifyvm", :id, "--clipboard-mode", "bidirectional"]
    vb.customize ["modifyvm", :id, "--draganddrop", "bidirectional"]
    vb.customize ["modifyvm", :id, "--usbxhci", "on"]
    vb.customize ["modifyvm", :id, "--audioin", "on"]
    vb.customize ["modifyvm", :id, "--audiocontroller", "hda"]
    vb.customize ["modifyvm", :id, "--vrde", "off"]
  end

  ############################################################################
  # File copy provisioners                                                   #
  ############################################################################
  config.vm.provision "file", source: "~/.ssh", destination: ".ssh"
  config.vm.provision "file", source: "~/.gitconfig", destination: ".gitconfig"

  ############################################################################
  # Shell script provisioner                                                 #
  ############################################################################
  config.vm.provision "shell", inline: <<-'SHELL'
    ############################################################################
    # Add Software                                                             #
    ############################################################################
    # Add EPEL
    echo -e "\nInstalling additional repos\n"
    dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm
    curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo >\
      "/etc/yum.repos.d/github-cli.repo"
    echo
    /usr/bin/crb enable

    # Software Update
    echo -e "\nInstalling Updates\n"
    dnf update -y

    echo -e "\nUpdating to NodeJS 20\n"
    dnf module -y reset nodejs
    dnf module -y enable nodejs:18
    dnf distro-sync -y

    # Install Container/Additional Software
    echo -e "\nInstalling additional software\n"
    dnf install -y podman skopeo podman-docker tmux tree \
      git git-lfs gh rsync mkisofs isomd5sum nodejs

    # Setup Rootless Podman
    echo -e "\nSetting up rootless podman"
    sysctl user.max_user_namespaces=15000
    sed -i 's/user.max_user_namespaces=0/user.max_user_namespaces=15000/i' /etc/sysctl.conf
    usermod --add-subuids 200000-201000 --add-subgids 200000-201000 vagrant
    groupadd -r docker
    usermod -aG docker vagrant
    touch /etc/containers/nodocker

    # Import .ssh to vagrant user
    echo -e "\nSetting up vagrant user .ssh"
    cp -v /root/.gitconfig /home/vagrant/
    git config --global http.sslBackend openssl
    cat /home/vagrant/.ssh/id*.pub >> /home/vagrant/.ssh/authorized_keys
    cat /root/.ssh/id*.pub >> /root/.ssh/authorized_keys
    chown -R vagrant:vagrant /home/vagrant
    chmod -R 600 /home/vagrant/.ssh
    chmod 700 /home/vagrant/.ssh

    # Install Yarn etc
    echo -e "\nInstall yarn & verdaccio"
    npm install -g verdaccio yarn corepack

    echo -e "\nUpdate NPM"
    npm install -g npm@10.8.2
    
    echo -e "\nDone Updating. Rebooting.\n"
  SHELL

  # Reboot to enable updates
  config.vm.provision 'shell', reboot: true

  # Enable FIPS
  config.vm.provision "shell", inline: <<-'SHELL'
    echo -e "\nEnabling FIPS\n"
    fips-mode-setup --enable
  SHELL
    
  # Reboot to enable FIPS
  config.vm.provision 'shell', reboot: true

  # Run Local routine to set up environment
  config.vm.provision "shell", privileged: false,
  env: {
    GITHUB_TOKEN:ENV['GITHUB_TOKEN']
  }, inline: <<-'SHELL'
    echo -e "\nRunning user provisioning\n"
    if [[ -z "$GITHUB_TOKEN" ]]; then
      echo "GITHUB_TOKEN undefined. Will Prompt authentication via browser"
      gh auth login
    else
      echo "GITHUB_TOKEN detected. Respecting token."
    fi
    export DATE=`date '+%Y%m%d-%H%M'`

    echo -e "\nUpdate Yarn"
    git config --global http.sslBackend openssl
    yarn set version 4.0.2

    echo -e "\nRunning local collection process\n"
    git clone https://github.com/amentumcms/Collector-Node-Web.git
    cd Collector-Node-Web && export XDG_DATA_HOME=$PWD
    echo -e "\nRunning verdaccio\n"
    yarn dlx verdaccio >\
      verdaccio/verdaccio.log 2>\
      verdaccio/verdaccio-err.log &
    tail -f verdaccio/verdaccio.log & P=$! && sleep 45 && kill -9 $P

    echo -e "\nTailing errors\n"
    tail verdaccio/verdaccio-err.log

    echo -e "\nSetting up yarn\n"
    npm set registry http://localhost:4873/
    yarn config set npmRegistryServer http://localhost:4873/
    echo -e "\nChecking Connectivity\n"
    curl http://localhost:4873 -o /dev/null
    sleep 1
    echo -e "\nRunning Yarn install"
    yarn install
    echo -e "\nListing verdaccio"
    ls -Alht verdaccio/*
    du -d1ch
    echo -e "\nListing Consumption"
    du -chd0 .yarn verdaccio node_modules

    # ./collect.sh
    # ./mkiso.sh
    
  SHELL

end
