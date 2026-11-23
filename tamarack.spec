# SPDX-License-Identifier: GPL-3.0-or-later
#
# Packaged per the Fedora Python Packaging Guidelines (pyproject-rpm-macros).
# https://docs.fedoraproject.org/en-US/packaging-guidelines/Python/

Name:           tamarack
Version:        0.1.0
Release:        1%{?dist}
Summary:        Self-healing storage planner for mixed-size drives

License:        GPL-3.0-or-later
URL:            https://github.com/theronconrey/tamarack
Source:         %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
# Generates the man page from the argparse definition.
BuildRequires:  /usr/bin/argparse-manpage
# %%check runs the engine's D-Bus tests on a private bus.
BuildRequires:  python3-gobject-base
BuildRequires:  dbus-daemon

BuildRequires:  systemd-rpm-macros

%description
Tamarack plans Drobo-style storage for mixed-size drives on Fedora, built from
stock tools: dm-integrity, mdadm, LVM and Btrfs. It picks the redundancy
level automatically from the drives present, lays drives out in SHR-style
layers, and plans adding drives one at a time.

The tamarack command only plans: it prints layouts, layout files and dry-run
command sequences, and never writes to a disk.

%package engine
Summary:        Tamarack storage engine service (D-Bus, polkit)
Requires:       %{name} = %{version}-%{release}
Requires:       python3-gobject-base
Requires:       polkit
Requires:       udisks2
Requires:       dbus-common
# The storage tools the engine runs (see tamarack/ops.py, ALLOWED_PROGRAMS).
Requires:       gdisk
Requires:       parted
Requires:       cryptsetup
Requires:       mdadm
Requires:       lvm2
Requires:       device-mapper
Requires:       btrfs-progs
Requires:       util-linux-core
Requires:       systemd
Recommends:     upower
%{?systemd_requires}

%description engine
The privileged half of Tamarack: a tightly confined systemd service on the
system bus that plans changes, has polkit authorize each one, and runs
approved plans step by step with a journal that survives power cuts.

Execution is off unless the engine configuration file in /etc/tamarack turns
it on. It is meant for test machines until the VM test matrix passes.

%prep
%autosetup -p1 -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires -x test

%build
%pyproject_wheel
PYTHONPATH=src argparse-manpage --module tamarack.cli --function build_parser \
    --project-name tamarack --prog tamarack --url %{url} --output tamarack.1

%install
%pyproject_install
%pyproject_save_files -l tamarack
install -Dpm 0644 tamarack.1 %{buildroot}%{_mandir}/man1/tamarack.1
# The engine is started by systemd, never by hand.
install -dm 0755 %{buildroot}%{_libexecdir}
mv %{buildroot}%{_bindir}/tamarack-engine %{buildroot}%{_libexecdir}/tamarack-engine
install -Dpm 0644 data/systemd/tamarack-engine.service \
    %{buildroot}%{_unitdir}/tamarack-engine.service
install -Dpm 0644 data/dbus/com.borealislogistics.Tamarack1.conf \
    %{buildroot}%{_datadir}/dbus-1/system.d/com.borealislogistics.Tamarack1.conf
install -Dpm 0644 data/dbus/com.borealislogistics.Tamarack1.service \
    %{buildroot}%{_datadir}/dbus-1/system-services/com.borealislogistics.Tamarack1.service
install -Dpm 0644 src/tamarack/com.borealislogistics.Tamarack1.xml \
    %{buildroot}%{_datadir}/dbus-1/interfaces/com.borealislogistics.Tamarack1.xml
install -Dpm 0644 data/polkit/com.borealislogistics.tamarack.policy \
    %{buildroot}%{_datadir}/polkit-1/actions/com.borealislogistics.tamarack.policy
install -Dpm 0644 data/modules-load/tamarack.conf \
    %{buildroot}%{_modulesloaddir}/tamarack.conf
install -dm 0755 %{buildroot}%{_sysconfdir}/tamarack

%check
%pyproject_check_import
# Test the installed package, not the source tree the dev config points at.
%pytest -o pythonpath=

%post engine
%systemd_post tamarack-engine.service

%preun engine
%systemd_preun tamarack-engine.service

%postun engine
%systemd_postun_with_restart tamarack-engine.service

%files -f %{pyproject_files}
%doc README.md CONTRIBUTING.md
%{_bindir}/tamarack
%{_mandir}/man1/tamarack.1*

%files engine
%doc docs/design/engine.md
%{_libexecdir}/tamarack-engine
%{_unitdir}/tamarack-engine.service
%{_datadir}/dbus-1/system.d/com.borealislogistics.Tamarack1.conf
%{_datadir}/dbus-1/system-services/com.borealislogistics.Tamarack1.service
%{_datadir}/dbus-1/interfaces/com.borealislogistics.Tamarack1.xml
%{_datadir}/polkit-1/actions/com.borealislogistics.tamarack.policy
%{_modulesloaddir}/tamarack.conf
%dir %{_sysconfdir}/tamarack

%changelog
* Sun Nov 22 2026 Theron Conrey <theron@conrey.org> - 0.1.0-1
- Initial package
