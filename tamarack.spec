# SPDX-License-Identifier: GPL-3.0-or-later
#
# Packaged per the Fedora Python Packaging Guidelines (pyproject-rpm-macros).
# https://docs.fedoraproject.org/en-US/packaging-guidelines/Python/

%global selinuxtype targeted

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
# Builds the SELinux policy module (selinux/).
BuildRequires:  selinux-policy-devel
BuildRequires:  bzip2

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
Requires:       parted
Requires:       integritysetup
Requires:       mdadm
Requires:       lvm2
Requires:       btrfs-progs
Requires:       util-linux-core
Requires:       systemd
# %%posttrans asks the running engine whether a job is running (busctl).
Requires(posttrans): systemd
# SMART monitoring (smartd, enabled by Fedora's presets) and the weekly md
# check (raid-check.timer, from mdadm).
Requires:       smartmontools
# Timeline snapshots of the shares (template: data/snapper/tamarack).
Requires:       snapper
# NFS shares; firewalld is recommended, not required, so a box without it
# still serves (the admin then opens port 2049 another way).
Requires:       nfs-utils
Recommends:     firewalld
Recommends:     upower
# The engine's own SELinux domain, wherever the targeted policy is in use.
Requires:       (%{name}-selinux if selinux-policy-%{selinuxtype})

%description engine
The privileged half of Tamarack: a tightly confined systemd service on the
system bus that plans changes, has polkit authorize each one, and runs
approved plans step by step with a journal that survives power cuts.

Execution is off unless the engine configuration file in /etc/tamarack turns
it on. It is meant for test machines until the VM test matrix passes.

%package selinux
Summary:        SELinux policy module for the Tamarack storage engine
Requires:       selinux-policy-%{selinuxtype}
Requires(post): selinux-policy-%{selinuxtype}
%{?_selinux_policy_version:Requires: selinux-policy >= %{_selinux_policy_version}}
%{?selinux_requires}

%description selinux
Confines the Tamarack storage engine to its own SELinux domain. The storage
tools it starts keep running in the domains Fedora already gives them, so
the engine is never granted their permissions.

%package cockpit
Summary:        Cockpit page for the Tamarack storage engine
Requires:       %{name}-engine = %{version}-%{release}
Requires:       cockpit-bridge

%description cockpit
The Tamarack page in Cockpit: whether your data is protected, the drives,
the space and how your data is looked after, in plain words. It talks only
to the Tamarack engine over D-Bus. This first version only shows; changes
are still made with "tamarack engine".

%prep
%autosetup -p1 -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires -x test

%build
%pyproject_wheel
make -C selinux -f %{_datadir}/selinux/devel/Makefile tamarack.pp
bzip2 -9 selinux/tamarack.pp
PYTHONPATH=src argparse-manpage --module tamarack.cli --function build_parser \
    --project-name tamarack --prog tamarack --url %{url} --output tamarack.1

%install
%pyproject_install
%pyproject_save_files -l tamarack
install -Dpm 0644 tamarack.1 %{buildroot}%{_mandir}/man1/tamarack.1
# The engine is started by systemd, never by hand.
install -dm 0755 %{buildroot}%{_libexecdir}
mv %{buildroot}%{_bindir}/tamarack-engine %{buildroot}%{_libexecdir}/tamarack-engine
mv %{buildroot}%{_bindir}/tamarack-meta %{buildroot}%{_libexecdir}/tamarack-meta
install -Dpm 0644 data/systemd/tamarack-engine.service \
    %{buildroot}%{_unitdir}/tamarack-engine.service
install -Dpm 0644 data/systemd/srv-tamarack.mount %{buildroot}%{_unitdir}/srv-tamarack.mount
install -Dpm 0644 data/systemd/tamarack-scrub.service %{buildroot}%{_unitdir}/tamarack-scrub.service
install -Dpm 0644 data/systemd/tamarack-scrub.timer %{buildroot}%{_unitdir}/tamarack-scrub.timer
install -Dpm 0644 data/dbus/io.github.theronconrey.Tamarack1.conf \
    %{buildroot}%{_datadir}/dbus-1/system.d/io.github.theronconrey.Tamarack1.conf
install -Dpm 0644 data/dbus/io.github.theronconrey.Tamarack1.service \
    %{buildroot}%{_datadir}/dbus-1/system-services/io.github.theronconrey.Tamarack1.service
install -Dpm 0644 src/tamarack/io.github.theronconrey.Tamarack1.xml \
    %{buildroot}%{_datadir}/dbus-1/interfaces/io.github.theronconrey.Tamarack1.xml
install -Dpm 0644 data/polkit/io.github.theronconrey.tamarack.policy \
    %{buildroot}%{_datadir}/polkit-1/actions/io.github.theronconrey.tamarack.policy
install -Dpm 0644 data/tmpfiles/tamarack.conf %{buildroot}%{_tmpfilesdir}/tamarack.conf
install -dm 0755 %{buildroot}%{_sysconfdir}/tamarack
install -Dpm 0644 data/snapper/tamarack \
    %{buildroot}%{_datadir}/snapper/config-templates/tamarack
install -Dpm 0644 selinux/tamarack.pp.bz2 \
    %{buildroot}%{_datadir}/selinux/packages/%{selinuxtype}/tamarack.pp.bz2
install -Dpm 0644 selinux/tamarack.if \
    %{buildroot}%{_datadir}/selinux/devel/include/distributed/tamarack.if
install -dm 0755 %{buildroot}%{_datadir}/cockpit/tamarack
install -pm 0644 data/cockpit/tamarack/manifest.json data/cockpit/tamarack/index.html \
    data/cockpit/tamarack/tamarack.css data/cockpit/tamarack/tamarack.js \
    data/cockpit/tamarack/status.js %{buildroot}%{_datadir}/cockpit/tamarack/

%check
%pyproject_check_import
# Test the installed package, not the source tree the dev config points at.
%pytest -o pythonpath=

%post engine
%systemd_post tamarack-engine.service tamarack-scrub.timer srv-tamarack.mount
%tmpfiles_create %{_tmpfilesdir}/tamarack.conf

%preun engine
%systemd_preun tamarack-engine.service tamarack-scrub.timer tamarack-scrub.service srv-tamarack.mount

%postun engine
# No restart here: an upgrade must never kill a storage command mid-job
# (the job would then wait for a person). See %%posttrans.
%systemd_postun tamarack-engine.service tamarack-scrub.timer tamarack-scrub.service srv-tamarack.mount

%posttrans engine
# Pick up the new version now only if the engine is running and no job is:
# idle, or paused waiting for a person (the journal carries the job across a
# restart). Otherwise it starts the new version after the job, at its next
# start. Nothing happens in an image build, where nothing runs.
if systemctl is-active --quiet tamarack-engine.service 2>/dev/null; then
    state=$(busctl --system get-property io.github.theronconrey.Tamarack1 \
        /io/github/theronconrey/Tamarack1 io.github.theronconrey.Tamarack1.Manager State 2>/dev/null || :)
    case "$state" in
        's "idle"'|'s "paused"')
            systemctl try-restart tamarack-engine.service >/dev/null 2>&1 || : ;;
        *)
            echo "tamarack-engine: a storage job is running; the new version starts after it, at the next restart." >&2 ;;
    esac
fi

%pre selinux
%selinux_relabel_pre -s %{selinuxtype}

%post selinux
%selinux_modules_install -s %{selinuxtype} %{_datadir}/selinux/packages/%{selinuxtype}/tamarack.pp.bz2

%postun selinux
if [ $1 -eq 0 ]; then
    %selinux_modules_uninstall -s %{selinuxtype} tamarack
fi

%posttrans selinux
%selinux_relabel_post -s %{selinuxtype}

%files -f %{pyproject_files}
%doc README.md
%{_bindir}/tamarack
%{_mandir}/man1/tamarack.1*

%files engine
%doc docs/design/engine.md
%{_libexecdir}/tamarack-engine
%{_libexecdir}/tamarack-meta
%{_unitdir}/tamarack-engine.service
%{_unitdir}/srv-tamarack.mount
%{_unitdir}/tamarack-scrub.service
%{_unitdir}/tamarack-scrub.timer
%{_datadir}/dbus-1/system.d/io.github.theronconrey.Tamarack1.conf
%{_datadir}/dbus-1/system-services/io.github.theronconrey.Tamarack1.service
%{_datadir}/dbus-1/interfaces/io.github.theronconrey.Tamarack1.xml
%{_datadir}/polkit-1/actions/io.github.theronconrey.tamarack.policy
%{_tmpfilesdir}/tamarack.conf
%{_datadir}/snapper/config-templates/tamarack
%ghost %config(noreplace) %{_sysconfdir}/nfs.conf.d/tamarack.conf
%dir %{_sysconfdir}/tamarack
%ghost %config(noreplace) %{_sysconfdir}/integritytab

%files selinux
%license LICENSE
%{_datadir}/selinux/packages/%{selinuxtype}/tamarack.pp.bz2
%{_datadir}/selinux/devel/include/distributed/tamarack.if
%ghost %attr(0644,root,root) %verify(not md5 size mode mtime) %{_sharedstatedir}/selinux/%{selinuxtype}/active/modules/200/tamarack

%files cockpit
%license LICENSE
%{_datadir}/cockpit/tamarack/

%changelog
* Sun Nov 22 2026 Theron Conrey <theron@conrey.org> - 0.1.0-1
- Initial package
